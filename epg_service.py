#!/usr/bin/env python3
import os
import csv
import logging
import threading
import time
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from xml.dom import minidom
import requests
from fastapi import FastAPI, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("IPTV-Service")

app = FastAPI(title="HTPC IPTV Proxy Engine")

CSV_MAP_PATH = "multicast_map.csv"
PROVIDER_API_BASE = "https://remotedvr.novusnow.ca/api/epg"
LOGO_BASE_URL = "https://remotedvr.novusnow.ca/images/channel_logo"
RTP2HTTPD_BASE = os.getenv("RTP2HTTPD_BASE")

CHANNEL_GROUPS = [
    ((100, 199), "Major Networks"),
    ((200, 299), "Out of Market"),
    ((300, 399), "Entertainment"),
    ((400, 499), "Movies & Series"),
    ((500, 599), "Comedy & Music"),
    ((600, 699), "Kids & Family"),
    ((700, 799), "Learning"),
    ((800, 899), "News"),
    ((900, 999), "Sports & PPV"),
    ((1000, 1999), "Multicultural"),
    ((3000, 3999), "Adult")
]

# Expanded Memory Cache Structure
epg_cache = {
    "last_refresh_date": None,  # Tracks the exact calendar day this cache block was built
    "channels": [],             # Unified deduplicated list of channel structures
    "schedules": [],            # Master aggregated list of program items across 9 days
    "lock": threading.Lock()
}

def load_network_map():
    net_map = {}
    if not os.path.exists(CSV_MAP_PATH):
        logger.error(f"Network map '{CSV_MAP_PATH}' missing.")
        return net_map
    with open(CSV_MAP_PATH, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            net_map[int(row["program_number"])] = {"ip": row["ip"], "port": row["port"]}
    return net_map

def determine_channel_group(tuner_pos: int) -> str:
    """Classifies a channel category group iterating through the custom tuple bounds."""
    for (lower, upper), group_name in CHANNEL_GROUPS:
        if lower <= tuner_pos <= upper:
            return group_name
    return "General"

def stream_proxy_url(stream: dict) -> str:
    """Constructs a proxy URL for the given RTP stream."""
    return f"{RTP2HTTPD_BASE}/rtp/{stream['ip']}:{stream['port']}"

def stream_rtp_url(stream: dict) -> str:
    """Constructs a direct RTP URL for the given stream."""
    return f"rtp://@{stream['ip']}:{stream['port']}"

def fetch_provider_epg():
    """Loops through a 9-day sliding window (-1 day to +7 days) to build a combined EPG matrix."""
    logger.info("Initiating multi-day sliding window EPG refresh...")

    headers = {"User-Agent": "Mozilla/5.0 HTPC-Proxy-Engine"}
    aggregated_channels = {}
    aggregated_schedules = []

    # Define our window boundaries (-1 to +7 equals 9 days total)
    today = datetime.now()
    start_offset = -1
    end_offset = 7

    for i in range(start_offset, end_offset + 1):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime("%Y/%m/%d")
        url = f"{PROVIDER_API_BASE}/{date_str}"

        logger.info(f"Fetching day offset {i:+} ({target_date.strftime('%Y-%m-%d')}) from API...")
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            day_data = response.json()

            # Extract and deduplicate structural channel entries
            for ch in day_data.get("channels", []):
                ch_id = ch.get("id")
                if ch_id and ch_id not in aggregated_channels:
                    aggregated_channels[ch_id] = ch

            # Accumulate schedule objects
            day_schedules = day_data.get("schedules", [])
            aggregated_schedules.extend(day_schedules)
            logger.debug(f"Successfully processed {len(day_schedules)} entries for {date_str}")

        except Exception as e:
            logger.error(f"Failed to fetch EPG segment for date {date_str}: {e}")
            # Continue trying to parse other days even if a single date endpoint drops/fails
            continue

    # Deconflict and commit atomic swap to the shared memory cache
    with epg_cache["lock"]:
        epg_cache["channels"] = list(aggregated_channels.values())
        epg_cache["schedules"] = aggregated_schedules
        epg_cache["last_refresh_date"] = today.date()

    logger.info(f"EPG Aggregation complete: Cached {len(epg_cache['channels'])} channels and {len(epg_cache['schedules'])} schedules.")

def epg_scheduler():
    """Background thread manager verifying freshness on a daily iteration rule."""
    while True:
        current_date = datetime.now().date()
        # If cache is bare or our reference tracking date rolls over past midnight, rebuild matrix
        if epg_cache["last_refresh_date"] is None or epg_cache["last_refresh_date"] != current_date:
            fetch_provider_epg()

        # Idle for 30 minutes between evaluation sweeps
        time.sleep(1800)

@app.on_event("startup")
def startup_event():
    thread = threading.Thread(target=epg_scheduler, daemon=True)
    thread.start()

@app.get("/playlist.m3u")
def get_playlist(proxy: bool = False):
    """Compiles the M3U playlist, sorting and including hidden unlisted multicast map streams."""
    if proxy and not RTP2HTTPD_BASE:
        return Response(content="#EXTM3U\n# RTP2HTTPD_BASE is not configured; cannot generate proxy playlist.", media_type="audio/x-mpegurl")

    with epg_cache["lock"]:
        api_channels = epg_cache["channels"]

    network_map = load_network_map()
    if not network_map:
        return Response(content="#EXTM3U\n# Local multicast map is missing or unreadable.", media_type="audio/x-mpegurl")

    m3u_lines = ["#EXTM3U\n"]

    # 1. Map known channels returned from the provider API
    api_map_positions = set()
    processed_channels = []

    for ch in api_channels:
        tuner_pos = ch.get("tunerPosition")
        if tuner_pos is not None:
            tuner_pos = int(tuner_pos)
            if tuner_pos in network_map:
                if proxy:
                    stream = stream_proxy_url(network_map[tuner_pos])
                else:
                    stream = stream_rtp_url(network_map[tuner_pos])
                api_map_positions.add(tuner_pos)
                processed_channels.append({
                    "tuner_position": tuner_pos,
                    "id": ch.get("id", f"unlinked-{tuner_pos}"),
                    "display_name": ch.get("description", ch.get("serviceCollectionName", f"Channel {tuner_pos}")),
                    "logo_url": f"{LOGO_BASE_URL}/{ch['callNumber']}.png" if ch.get("callNumber") else "",
                    "stream": stream
                })

    # 2. Fallback: Find raw streams discovered via network probe that were skipped by the API
    for raw_tuner_pos, stream_info in network_map.items():
        if raw_tuner_pos not in api_map_positions:
            group_name = determine_channel_group(raw_tuner_pos)
            logger.info(f"Synthesizing unlisted fallback definition for Channel {raw_tuner_pos} ({group_name})")
            if proxy:
                stream = stream_proxy_url(stream_info)
            else:
                stream = stream_rtp_url(stream_info)
            processed_channels.append({
                "tuner_position": raw_tuner_pos,
                "id": f"raw-{raw_tuner_pos}",
                "display_name": f"Unlisted Channel {raw_tuner_pos}",
                "logo_url": "",  # No call number to extrapolate logo from
                "stream": stream
            })

    # 3. Sort the combined structural list numerically by channel number
    processed_channels.sort(key=lambda x: x["tuner_position"])

    # 4. Generate M3U file lines
    for ch in processed_channels:
        group_title = determine_channel_group(ch["tuner_position"])
        stream = ch["stream"]

        m3u_lines.append(
            f'#EXTINF:0 tvg-chno="{ch["tuner_position"]}" tvg-logo="{ch["logo_url"]}" tvg-id="{ch["id"]}" group-title="{group_title}",{ch["display_name"]}\n'
            f'{stream}\n'
        )

    return Response(content="".join(m3u_lines), media_type="audio/x-mpegurl")

@app.get("/epg.xml")
def get_xmltv():
    """Translates the consolidated sliding schedule array into a formal XMLTV layout."""
    with epg_cache["lock"]:
        channels = epg_cache["channels"]
        schedules = epg_cache["schedules"]

    if not channels and not schedules:
        return Response(content='<?xml version="1.0" encoding="utf-8"?><tv></tv>', media_type="application/xml")

    tv_root = ET.Element("tv", {
        "generator-info-name": "Custom IPTV Proxy Engine",
        "generator-info-url": "http://localhost:9090/"
    })

    # 1. Map Channel metadata headers
    for ch in channels:
        ch_id = ch.get("id")
        if not ch_id:
            continue
        channel_node = ET.SubElement(tv_root, "channel", id=str(ch_id))
        ET.SubElement(channel_node, "display-name").text = ch.get("description", ch.get("serviceCollectionName"))
        if ch.get("callNumber"):
            ET.SubElement(channel_node, "display-name").text = ch["callNumber"]
            ET.SubElement(channel_node, "icon", src=f"{LOGO_BASE_URL}/{ch['callNumber']}.png")

    # 2. Map Consolidated Look-behind and Look-ahead Program Items
    # Deduplicate entries using an internal identity key to guard against overlaps near day boundaries
    processed_program_keys = set()
    tz_offset = time.strftime("%z") or "+0000"

    for sched in schedules:
        ch_id = sched.get("channelId")
        start_ms = sched.get("startDateCal")
        duration_secs = sched.get("duration")
        program_key = sched.get("id")  # Unique key structure from provider api (e.g. "92160-1781766000")

        if not ch_id or not start_ms or not duration_secs or not program_key:
            continue

        if program_key in processed_program_keys:
            continue
        processed_program_keys.add(program_key)

        start_dt = datetime.fromtimestamp(start_ms / 1000.0)
        end_dt = start_dt + timedelta(seconds=int(duration_secs))

        xmltv_start = f"{start_dt.strftime('%Y%m%d%H%M%S')} {tz_offset}"
        xmltv_end = f"{end_dt.strftime('%Y%m%d%H%M%S')} {tz_offset}"

        programme_node = ET.SubElement(tv_root, "programme", {
            "start": xmltv_start,
            "stop": xmltv_end,
            "channel": str(ch_id)
        })

        ET.SubElement(programme_node, "title", lang="en").text = sched.get("title", "No Title")
        if sched.get("longTitle") and sched["longTitle"] != sched.get("title"):
            # Handle split formatting for subtitles safely
            title_parts = sched["longTitle"].split("/")
            if len(title_parts) > 1:
                ET.SubElement(programme_node, "sub-title", lang="en").text = title_parts[-1]

    raw_xml = ET.tostring(tv_root, encoding="utf-8")
    parsed_xml = minidom.parseString(raw_xml)
    pretty_xml = parsed_xml.toprettyxml(indent="  ", encoding="utf-8")

    return Response(content=pretty_xml, media_type="application/xml")
