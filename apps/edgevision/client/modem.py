"""
Modem radio data poller — TLT Networks CPE with Quectel RG501Q-EU.

Polls /api/modems/status for SINR, RSRP, RSRQ, band, connection type.
Auto-refreshes auth token on expiry.
"""

import logging
import threading
import time

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

MODEM_URL = "https://192.168.1.1"
MODEM_USER = "admin"
MODEM_PASS = "x7SBj25G!"
POLL_INTERVAL = 2.0

from typing import Optional
_token: Optional[str] = None
_session: Optional[requests.Session] = None
_radio_data: dict = {}
_lock = threading.Lock()
_running = False


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.verify = False
    return _session


def _login() -> bool:
    global _token
    try:
        s = _get_session()
        resp = s.post(f"{MODEM_URL}/api/login", json={
            "username": MODEM_USER,
            "password": MODEM_PASS,
        }, timeout=5)
        data = resp.json()
        if data.get("success"):
            _token = data["data"]["token"]
            return True
    except Exception as e:
        logger.warning(f"Modem login failed: {e}")
    return False


def _poll_once() -> Optional[dict]:
    global _token
    if not _token:
        if not _login():
            return None

    try:
        s = _get_session()
        resp = s.get(f"{MODEM_URL}/api/modems/status",
                     headers={"Authorization": f"Bearer {_token}"},
                     timeout=5)
        if resp.status_code == 401:
            _token = None
            if _login():
                resp = s.get(f"{MODEM_URL}/api/modems/status",
                             headers={"Authorization": f"Bearer {_token}"},
                             timeout=5)
            else:
                return None

        data = resp.json()
        if not data.get("success") or not data.get("data"):
            return None

        modem = data["data"][0]
        cell = modem.get("cell_info", [{}])[0] if modem.get("cell_info") else {}

        return {
            "conntype": modem.get("conntype", "N/A"),
            "band": modem.get("band", "N/A"),
            "bandwidth": cell.get("bandwidth", "N/A"),
            "sinr": modem.get("sinr", "N/A"),
            "rsrp": modem.get("rsrp", "N/A"),
            "rsrq": modem.get("rsrq", "N/A"),
            "rssi": modem.get("rssi", "N/A"),
            "cellid": modem.get("cellid", "N/A"),
            "pci": cell.get("pcid", "N/A"),
            "temp": modem.get("temperature", "N/A"),
        }
    except Exception as e:
        logger.warning(f"Modem poll failed: {e}")
        return None


def _poll_loop():
    global _radio_data, _running
    while _running:
        result = _poll_once()
        if result:
            with _lock:
                _radio_data = result
        time.sleep(POLL_INTERVAL)


def start():
    global _running
    _running = True
    threading.Thread(target=_poll_loop, daemon=True).start()


def stop():
    global _running
    _running = False


def get_radio_data() -> dict:
    with _lock:
        return _radio_data.copy()
