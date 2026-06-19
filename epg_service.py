#!/usr/bin/env python3
import os
import csv
import json
import logging
import threading
import time
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from xml.dom import minidom
import requests
from fastapi import FastAPI, Response

# Setup Logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("IPTV-Service")

app = FastAPI(title="HTPC IPTV Proxy Engine")

# Configuration
CSV_MAP_PATH = "multicast_map.csv"
PROVIDER_API_BASE = "https://remotedvr.novusnow.ca/api/epg"
LOGO_BASE_URL = "https://remotedvr.novusnow.ca/images/channel_logo"

# Thread-safe global memory cache
epg_cache = {"date_fetched": None, "raw_json_data": None, "lock": threading.Lock()}


def load_network_map():
    """Loads CSV mappings into a dictionary keyed by Program/Tuner number."""
    net_map = {}
    if not os.path.exists(CSV_MAP_PATH):
        logger.error(
            f"Network map '{CSV_MAP_PATH}' missing. M3U compilation will fail."
        )
        return net_map

    with open(CSV_MAP_PATH, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            net_map[int(row["program_number"])] = {"ip": row["ip"], "port": row["port"]}
    return net_map


def fetch_provider_epg():
    """Worker function to fetch the API payload and refresh the global cache."""
    today_str = datetime.now().strftime("%Y/%m/%d")
    url = f"{PROVIDER_API_BASE}/{today_str}"
    logger.info(f"Refreshing cache. Querying provider API: {url}")

    try:
        headers = {"User-Agent": "Mozilla/5.0 HTPC-Proxy-Engine"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()

        with epg_cache["lock"]:
            epg_cache["raw_json_data"] = payload
            epg_cache["date_fetched"] = datetime.now().date()
        logger.info("EPG cache successfully updated.")
    except Exception as e:
        logger.error(f"Error updating EPG cache: {e}")


def epg_scheduler():
    """Background thread loop that ensures data updates once a day."""
    while True:
        current_date = datetime.now().date()
        # If cache is empty or it's a new day, fetch fresh data
        if (
            epg_cache["raw_json_data"] is None
            or epg_cache["date_fetched"] != current_date
        ):
            fetch_provider_epg()

        # Sleep for an hour before evaluating state again
        time.sleep(3600)


@app.on_event("startup")
def startup_event():
    """Spawns the background synchronization thread on app startup."""
    thread = threading.Thread(target=epg_scheduler, daemon=True)
    thread.start()


@app.get("/playlist.m3u")
def get_playlist():
    """Dynamically compiles the M3U playlist, sorted by channel number."""
    with epg_cache["lock"]:
        data = epg_cache["raw_json_data"]

    if not data or "channels" not in data:
        return Response(
            content="#EXTM3U\n# Cache warming up or provider API unreachable.",
            media_type="audio/x-mpegurl"
        )

    network_map = load_network_map()
    m3u_lines = ["#EXTM3U\n"]

    # Filter down to channels that have a valid tunerPosition and exist in our network map
    active_channels = []
    for ch in data["channels"]:
        tuner_pos = ch.get("tunerPosition")
        if tuner_pos is not None and int(tuner_pos) in network_map:
            active_channels.append(ch)

    # Sort the channels numerically by their tunerPosition
    active_channels.sort(key=lambda x: int(x["tunerPosition"]))

    # Build out the sorted M3U structure
    for ch in active_channels:
        tuner_pos = int(ch["tunerPosition"])
        stream = network_map[tuner_pos]
        display_name = ch.get("description", ch.get("serviceCollectionName", "Unknown Channel"))
        call_num = ch.get("callNumber", "")
        logo_url = f"{LOGO_BASE_URL}/{call_num}.png" if call_num else ""
        ch_id = ch.get("id")

        m3u_lines.append(
            f'#EXTINF:0 tvg-chno="{tuner_pos}" tvg-logo="{logo_url}" tvg-id="{ch_id}",{display_name}\n'
            f'rtp://@{stream["ip"]}:{stream["port"]}\n'
        )

    return Response(content="".join(m3u_lines), media_type="audio/x-mpegurl")


@app.get("/epg.xml")
def get_xmltv():
    """Translates the structured JSON metadata into standard XMLTV formatting."""
    with epg_cache["lock"]:
        data = epg_cache["raw_json_data"]

    if not data:
        return Response(content="<tv></tv>", media_type="application/xml")

    # Build standard XMLTV root elements
    tv_root = ET.Element(
        "tv",
        {
            "generator-info-name": "Custom IPTV Proxy Engine",
            "generator-info-url": "http://localhost:8000/",
        },
    )

    # 1. Append Channel Nodes
    for ch in data.get("channels", []):
        ch_id = ch.get("id")
        if not ch_id:
            continue

        channel_node = ET.SubElement(tv_root, "channel", id=str(ch_id))

        display_name = ch.get(
            "description", ch.get("serviceCollectionName", "Unknown Channel")
        )
        ET.SubElement(channel_node, "display-name").text = display_name

        if ch.get("callNumber"):
            ET.SubElement(channel_node, "display-name").text = ch["callNumber"]
            logo_url = f"{LOGO_BASE_URL}/{ch['callNumber']}.png"
            ET.SubElement(channel_node, "icon", src=logo_url)

    # 2. Append Program Guide (Schedules) Nodes
    for sched in data.get("schedules", []):
        ch_id = sched.get("channelId")
        start_ms = sched.get("startDateCal")
        duration_secs = sched.get("duration")

        if not ch_id or not start_ms or not duration_secs:
            continue

        # Parse epochs to XMLTV required date string format: YYYYMMDDhhmmss +HHMM
        start_dt = datetime.fromtimestamp(start_ms / 1000.0)
        end_dt = start_dt + timedelta(seconds=int(duration_secs))

        # Pull system timezone offset safely
        tz_offset = time.strftime("%z")
        if not tz_offset:
            tz_offset = "+0000"

        xmltv_start = f"{start_dt.strftime('%Y%m%d%H%M%S')} {tz_offset}"
        xmltv_end = f"{end_dt.strftime('%Y%m%d%H%M%S')} {tz_offset}"

        programme_node = ET.SubElement(
            tv_root,
            "programme",
            {"start": xmltv_start, "stop": xmltv_end, "channel": str(ch_id)},
        )

        # Add titles and titles variations
        ET.SubElement(programme_node, "title", lang="en").text = sched.get(
            "title", "No Title"
        )
        if sched.get("longTitle") and sched["longTitle"] != sched.get("title"):
            ET.SubElement(programme_node, "sub-title", lang="en").text = sched[
                "longTitle"
            ].split("/")[-1]

        # Descriptive blocks
        s_obj = sched.get("sObj", {})
        if s_obj and isinstance(s_obj, dict):
            # Fallback if specific descriptions are tucked in internal serialized structures
            pass

    # Beautify XML structure output
    raw_xml = ET.tostring(tv_root, encoding="utf-8")
    parsed_xml = minidom.parseString(raw_xml)
    pretty_xml = parsed_xml.toprettyxml(indent="  ", encoding="utf-8")

    return Response(content=pretty_xml, media_type="application/xml")


# ... (Keep all your existing FastAPI code above intact)


def main():
    """Execution entrypoint for the application runner."""
    import uvicorn

    uvicorn.run("epg_service:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
