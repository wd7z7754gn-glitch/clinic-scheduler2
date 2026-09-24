"""
clinic_scheduler.py - 診所排班查詢與預約工具
支援北極星系統 (YouKnow 行銷大師)
作者: 江家玲帳號使用
"""

import requests
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── 站點設定 ──────────────────────────────────────────
SITES = {
    "site1": {
        "name": "南屯一日誠臻牙醫診所",
        "base_url": "http://220.135.0.149:60771",
        "doctors": {
            "吳燕城": 5,
            "支援醫師": 6,
            "殷煜軒": 13,
            "陳函君": 14,
            "王威傑": 15,
            "陳琮愷": 16,
            "曾梓豪": 17,
        },
    },
    "site2": {
        "name": "大里一日誠臻牙醫診所",
        "base_url": "http://114.35.78.163:60772",
        "doctors": {
            "殷煜軒": 5,
            "支援醫師": 6,
            "廖科深": 7,
            "葉芃樓": 8,
            "賴裕祥": 9,
            "謝宇捷": 10,
            "陳琮愷": 11,
            "盧昱均": 22,
        },
    },
}

LOGIN_USER = "A0010"
LOGIN_PASS = "107922"

# 台灣時區 UTC+8
TW_TZ = timezone(timedelta(hours=8))

# 預設門診設定 (從伺服器動態取得覆蓋)
_DEFAULT_CLINIC_CFG = {
    "clinic_start": "09:00",
    "clinic_end": "21:30",
    "break_times": [("12:15", "13:45"), ("17:00", "18:00")],
}

# 快取：{base_url+dr_id: clinic_cfg}
_clinic_cfg_cache: dict = {}


# ── 工具函式 ──────────────────────────────────────────

def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _fetch_clinic_cfg(session: requests.Session, base_url: str, doctor_id: int) -> dict:
    """從 agendaWeekjs.php 動態取得診間設定 (開診/結診/休息時間)"""
    cache_key = f"{base_url}:{doctor_id}"
    if cache_key in _clinic_cfg_cache:
        return _clinic_cfg_cache[cache_key]

    import re
    cfg = dict(_DEFAULT_CLINIC_CFG)
    try:
        resp = session.get(
            f"{base_url}/appom/js/agendaWeekjs.php",
            params={"view": "agendaWeek", "cussn": "0", "maindr": doctor_id,
                    "year": "2026", "month": "8", "day": "24"},
            timeout=10,
        )
        text = resp.text

        def extract(key):
            m = re.search(rf"{key}\s*:\s*'([^']+)'", text)
            return m.group(1) if m else None

        cs = extract("clinicStart")
        ce = extract("clinicEnd")
        lbs = extract("lbreakTimeStart")
        lbe = extract("lbreakTimeEnd")
        dbs = extract("dbreakTimeStart")
        dbe = extract("dbreakTimeEnd")

        if cs:
            cfg["clinic_start"] = cs
        if ce:
            cfg["clinic_end"] = ce

        breaks = []
        if lbs and lbe:
            breaks.append((lbs, lbe))
        if dbs and dbe:
            breaks.append((dbs, dbe))
        if breaks:
            cfg["break_times"] = breaks

    except Exception:
        pass

    _clinic_cfg_cache[cache_key] = cfg
    return cfg


def _is_in_break(slot_start_min: int, slot_end_min: int, break_times: list) -> bool:
    for bs, be in break_times:
        bs_m = _time_to_minutes(bs)
        be_m = _time_to_minutes(be)
        if slot_start_min < be_m and slot_end_min > bs_m:
            return True
    return False


def _generate_slots(date: datetime, clinic_cfg: dict) -> list[dict]:
    """產生某天所有 15 分鐘時段 (依診間設定排除休息時間)"""
    start_m = _time_to_minutes(clinic_cfg["clinic_start"])
    end_m = _time_to_minutes(clinic_cfg["clinic_end"])
    break_times = clinic_cfg["break_times"]
    slots = []
    cur = start_m
    while cur + 15 <= end_m:
        if not _is_in_break(cur, cur + 15, break_times):
            h, m = divmod(cur, 60)
            slots.append({
                "date": date.strftime("%Y-%m-%d"),
                "time": f"{h:02d}:{m:02d}",
                "start_min": cur,
            })
        cur += 15
    return slots


def _login(base_url: str) -> Optional[requests.Session]:
    """登入並回傳已認證的 Session，失敗回傳 None"""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 ClinicScheduler/1.0"})
    try:
        resp = session.post(
            f"{base_url}/common/login.php",
            data={"uname": LOGIN_USER, "pass": LOGIN_PASS, "op": "chklogin", "redir": ""},
            timeout=15,
            allow_redirects=True,
        )
        # 登入成功會跳轉到 commentary.php
        if "commentary.php" in resp.url or "appointment.php" in resp.url:
            return session
        return None
    except Exception:
        return None


def _get_events(session: requests.Session, base_url: str, doctor_id: int,
                start_dt: datetime, end_dt: datetime) -> list[dict]:
    """取得指定醫師在時間範圍內的所有預約事件"""
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    try:
        resp = session.get(
            f"{base_url}/appom/ajax/getEvent.php",
            params={"start": start_ts, "end": end_ts, "active": "", "maindr": doctor_id},
            timeout=15,
        )
        return resp.json()
    except Exception:
        return []


def _events_to_blocked_slots(events: list[dict], date: datetime) -> set[int]:
    """將事件轉換成被佔用的 start_min 集合"""
    blocked = set()
    day_start = int(datetime(date.year, date.month, date.day, tzinfo=TW_TZ).timestamp())
    day_end = day_start + 86400

    for ev in events:
        ev_start = ev.get("start", 0)
        ev_end = ev.get("end", 0)
        # 只處理當天的事件
        if ev_end <= day_start or ev_start >= day_end:
            continue
        # 轉成當天的分鐘偏移量
        local_start_min = (ev_start - day_start) // 60
        local_end_min = (ev_end - day_start) // 60
        # 標記所有被覆蓋的 15 分鐘格
        cur = (local_start_min // 15) * 15
        while cur < local_end_min:
            blocked.add(cur)
            cur += 15
    return blocked


# ── 內部：病患搜尋 ───────────────────────────────────

def _find_patient(session: requests.Session, base_url: str, doctor_id: int,
                  name: str = "", phone: str = "") -> Optional[dict]:
    """依姓名或電話搜尋既有病患，回傳 {cussn, cusno, name} 或 None"""
    import re
    try:
        resp = session.post(
            f"{base_url}/ajax/search_pat.php",
            params={"op": ""},
            data={
                "maindr": str(doctor_id),
                "searchtype": "",
                "year": "2026", "month": "1", "day": "1",
                "chainCsn": "0",
                "cusbirthday": "",
                "cusname": name,
                "cusno": "",
                "custel": phone,
                "cusid": "",
                "lastdate": "",
            },
            timeout=15,
        )
        html = resp.text
        # 從 rel 屬性取出病患資料: cussn|cusno|name|...
        matches = re.findall(r"rel='([^']+)'", html)
        for m in matches:
            parts = m.split("|")
            if len(parts) >= 3 and parts[0].isdigit() and int(parts[0]) > 0:
                return {"cussn": int(parts[0]), "cusno": parts[1], "name": parts[2]}
        return None
    except Exception:
        return None


# ── 公開 API ──────────────────────────────────────────

def search_availability(doctor_name: str, available_days: int = 3, search_range: int = 60) -> dict:
    """
    查詢醫師「未來有空的 N 天」，回傳那幾天的所有空閒時段。

    Args:
        doctor_name:    醫師姓名 (例如: "吳燕城")
        available_days: 要找幾個有空的天數 (預設 3)
        search_range:   最多往前找幾天 (預設 60)

    Returns:
        {
          "success": bool,
          "doctor": str,
          "available_days_count": int,
          "available_slots": [
            {
              "site": str,
              "site_name": str,
              "date": "YYYY-MM-DD",
              "weekday": str,
              "time": "HH:MM",
              "datetime": "YYYY-MM-DD HH:MM"
            }, ...
          ],
          "error": str  # 僅在 success=False 時出現
        }
    """
    now = datetime.now(TW_TZ)
    WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

    found_in_any_site = False

    # 每個院所各自建立 session 與診間設定
    site_sessions = {}
    for site_key, site_cfg in SITES.items():
        dr_id = site_cfg["doctors"].get(doctor_name)
        if dr_id is None:
            continue
        found_in_any_site = True
        session = _login(site_cfg["base_url"])
        if session is None:
            continue
        clinic_cfg = _fetch_clinic_cfg(session, site_cfg["base_url"], dr_id)
        site_sessions[site_key] = {"session": session, "dr_id": dr_id, "clinic_cfg": clinic_cfg}

    if not found_in_any_site:
        return {
            "success": False,
            "doctor": doctor_name,
            "available_slots": [],
            "error": f"找不到醫師 '{doctor_name}'，請確認姓名是否正確。",
        }

    # 逐週取得事件（一次抓 7 天避免過多請求）
    start_dt = datetime(now.year, now.month, now.day, tzinfo=TW_TZ)
    days_found = 0           # 已找到幾個有空的天
    all_slots = []           # 所有空閒時段
    found_dates = set()      # 已計入的日期 (跨院所同一天只算一次)

    # 分批查詢，每批 14 天
    batch = 0
    while days_found < available_days and batch * 14 < search_range:
        batch_start = start_dt + timedelta(days=batch * 14)
        batch_end = batch_start + timedelta(days=14)

        # 每個院所取這 14 天的事件
        site_events = {}
        for site_key, info in site_sessions.items():
            evs = _get_events(info["session"], SITES[site_key]["base_url"],
                              info["dr_id"], batch_start, batch_end)
            site_events[site_key] = evs

        # 逐日檢查
        for d in range(14):
            if days_found >= available_days:
                break
            target_date = batch_start + timedelta(days=d)
            date_str = target_date.strftime("%Y-%m-%d")
            weekday_str = WEEKDAY_NAMES[target_date.weekday()]

            day_slots = []  # 這天所有院所的空閒時段
            for site_key, info in site_sessions.items():
                clinic_cfg = info["clinic_cfg"]
                blocked = _events_to_blocked_slots(site_events[site_key], target_date)
                slots = _generate_slots(target_date, clinic_cfg)

                for slot in slots:
                    slot_dt = datetime(
                        target_date.year, target_date.month, target_date.day,
                        *divmod(slot["start_min"], 60), tzinfo=TW_TZ
                    )
                    if slot_dt <= now:
                        continue
                    if slot["start_min"] not in blocked:
                        end_min = slot["start_min"] + 15
                        eh, em = divmod(end_min, 60)
                        end_time = f"{eh:02d}:{em:02d}"
                        day_slots.append({
                            "site": site_key,
                            "site_name": SITES[site_key]["name"],
                            "date": date_str,
                            "weekday": f"週{weekday_str}",
                            "time": f"{slot['time']} ~ {end_time}",
                            "datetime": f"{date_str} {slot['time']} ~ {end_time}",
                        })

            if day_slots:
                all_slots.extend(day_slots)
                if date_str not in found_dates:
                    found_dates.add(date_str)
                    days_found += 1

        batch += 1

    return {
        "success": True,
        "doctor": doctor_name,
        "available_days_count": days_found,
        "available_slots": all_slots,
    }


def book_appointment(
    doctor_name: str,
    date: str,
    time: str,
    patient_name: str = "",
    patient_phone: str = "",
    duration_mins: int = 15,
    site: Optional[str] = None,
    note: str = "",
) -> dict:
    """
    替患者預約診間時段。

    Args:
        doctor_name:   醫師姓名
        date:          預約日期 "YYYY-MM-DD"
        time:          預約時間 "HH:MM"
        patient_name:  患者姓名 (新患者必填)
        patient_phone: 患者行動電話
        duration_mins: 預計療程時間 (分鐘，預設 15)
        site:          指定站點 "site1" 或 "site2"（不填則自動選第一個有此醫師的站）
        note:          預約備註

    Returns:
        {
          "success": bool,
          "message": str,
          "booking_detail": { ... }  # 成功時回傳
        }
    """
    h, m = map(int, time.split(":"))

    for site_key, site_cfg in SITES.items():
        if site and site_key != site:
            continue
        dr_id = site_cfg["doctors"].get(doctor_name)
        if dr_id is None:
            continue

        session = _login(site_cfg["base_url"])
        if session is None:
            return {"success": False, "message": f"無法登入 {site_cfg['name']}"}

        # 先嘗試查找既有病患 (by phone > by name)
        found_patient = None
        if patient_phone:
            found_patient = _find_patient(session, site_cfg["base_url"], dr_id, phone=patient_phone)
        if not found_patient and patient_name:
            found_patient = _find_patient(session, site_cfg["base_url"], dr_id, name=patient_name)

        if found_patient:
            cussn_val = str(found_patient["cussn"])
            resolved_name = found_patient["name"]
            is_new_patient = False
        else:
            cussn_val = "0"
            resolved_name = patient_name
            is_new_patient = True

        payload = {
            "lregsn": "0",
            "seqno": "000",
            "st_seqno": "000",
            "chgcussn": "no",
            "cussn": cussn_val,
            "cusno": found_patient["cusno"] if found_patient else "",
            "newPatientName": "" if found_patient else patient_name,
            "schtel": patient_phone,
            "tel": "",
            "drno1": str(dr_id),
            "ddate": date,
            "sch_time1": f"{h:02d}",
            "sch_time2": f"{m:02d}",
            "schqty": "1",
            "schlen": str(duration_mins),
            "seltime": str(duration_mins),
            "sch_note": note if note else "預約",   # 必填：預約事項
            "note": "",
            "colorsn": "99999",
            "notify_flag": "0",
            "urgent": "0",
            "userid": "",
            "ischgmob": "0",
        }

        try:
            resp = session.post(
                f"{site_cfg['base_url']}/appom/ajax/saveAppom.php",
                data=payload,
                timeout=15,
            )
            result_text = resp.text.strip()

            # 系統成功時回傳含 regsn 的結果或空字串
            is_success = (
                "success" in result_text.lower()
                or result_text == ""
                or result_text.startswith("OK")
                or (result_text and result_text[0].isdigit())  # regsn
            )

            if is_success:
                return {
                    "success": True,
                    "message": f"預約成功！{site_cfg['name']} / {doctor_name} / {date} {time}",
                    "booking_detail": {
                        "site": site_key,
                        "site_name": site_cfg["name"],
                        "doctor": doctor_name,
                        "date": date,
                        "time": time,
                        "duration_mins": duration_mins,
                        "patient_name": resolved_name,
                        "patient_phone": patient_phone,
                        "is_new_patient": is_new_patient,
                        "server_response": result_text,
                    },
                }
            else:
                return {
                    "success": False,
                    "message": f"預約失敗：{result_text}",
                    "server_response": result_text,
                }
        except Exception as e:
            return {"success": False, "message": f"連線錯誤：{e}"}

    return {
        "success": False,
        "message": f"找不到醫師 '{doctor_name}' 在指定站點，或登入失敗。",
    }


def list_doctors() -> dict:
    """列出所有站點的醫師清單"""
    result = {}
    for site_key, site_cfg in SITES.items():
        result[site_key] = {
            "site_name": site_cfg["name"],
            "doctors": list(site_cfg["doctors"].keys()),
        }
    return result


# ── CLI 介面 ──────────────────────────────────────────

def _cli():
    import argparse

    # Windows 終端強制 UTF-8 輸出
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="診所排班查詢與預約工具")
    sub = parser.add_subparsers(dest="cmd")

    # search
    p_search = sub.add_parser("search", help="查詢醫師空閒時段")
    p_search.add_argument("doctor", help="醫師姓名")
    p_search.add_argument("--days", type=int, default=3, help="查詢天數 (預設3)")

    # book
    p_book = sub.add_parser("book", help="預約診間")
    p_book.add_argument("doctor", help="醫師姓名")
    p_book.add_argument("date", help="日期 YYYY-MM-DD")
    p_book.add_argument("time", help="時間 HH:MM")
    p_book.add_argument("--patient", default="", help="患者姓名")
    p_book.add_argument("--phone", default="", help="患者電話")
    p_book.add_argument("--duration", type=int, default=15, help="療程分鐘數")
    p_book.add_argument("--site", choices=["site1", "site2"], help="指定站點")
    p_book.add_argument("--note", default="", help="備註")

    # list
    sub.add_parser("list", help="列出所有醫師")

    args = parser.parse_args()

    if args.cmd == "search":
        result = search_availability(args.doctor, args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "book":
        result = book_appointment(
            args.doctor, args.date, args.time,
            patient_name=args.patient,
            patient_phone=args.phone,
            duration_mins=args.duration,
            site=args.site,
            note=args.note,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "list":
        result = list_doctors()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
