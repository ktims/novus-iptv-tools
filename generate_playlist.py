#!/usr/bin/env python3
import json
import csv
import urllib.request
from datetime import datetime

# Configuration
CSV_MAP = "multicast_map.csv"
OUTPUT_M3U = "iptv_playlist.m3u"
LOGO_BASE_URL = "https://remotedvr.novusnow.ca/images/channel_logo"

def get_api_url():
    """Generates the dynamic EPG API URL based on today's date."""
    today = datetime.now()
    return f"https://remotedvr.novusnow.ca/api/epg/{today.strftime('%Y/%m/%d')}"

def load_network_map(csv_path):
    """Loads CSV mappings into a dictionary keyed by Program/Tuner number."""
    net_map = {}
    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Map program_number to the networking tuple
                net_map[int(row["program_number"])] = {
                    "ip": row["ip"],
                    "port": row["port"]
                }
    except FileNotFoundError:
        print(f"Error: Could not find '{csv_path}'. Please run the prober script first.")
        exit(1)
    return net_map

def main():
    # 1. Load the localized network probe data
    print(f"Loading local network map from {CSV_MAP}...")
    network_map = load_network_map(CSV_MAP)
    
    # 2. Fetch the upstream channel catalog
    url = get_api_url()
    print(f"Fetching provider channel definitions from {url}...")
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
    except Exception as e:
        print(f"Failed to fetch data from provider API: {e}")
        exit(1)
        
    channels = data.get("channels", [])
    if not channels:
        print("No channel structural data found in the API response.")
        exit(1)
        
    # 3. Match and assemble the M3U entries
    print(f"Processing {len(channels)} API channels against network map...")
    m3u_lines = ["#EXTM3U\n"]
    matched_count = 0
    
    for ch in channels:
        tuner_pos = ch.get("tunerPosition")
        if tuner_pos is None:
            continue
            
        tuner_pos = int(tuner_pos)
        
        # If the tunerPosition matches a mapped program number from our network probe
        if tuner_pos in network_map:
            stream_info = network_map[tuner_pos]
            
            # Extract metadata details
            display_name = ch.get("description", ch.get("serviceCollectionName", "Unknown Channel"))
            call_num = ch.get("callNumber", "")
            logo_url = f"{LOGO_BASE_URL}/{call_num}.png" if call_num else ""
            
            # Construct standard M3U entry
            m3u_lines.append(
                f'#EXTINF:0 tvg-chno="{tuner_pos}" tvg-logo="{logo_url}" tvg-id="{ch.get("id")}",{display_name}\n'
                f'rtp://@{stream_info["ip"]}:{stream_info["port"]}\n'
            )
            matched_count += 1

    # 4. Save to file
    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.writelines(m3u_lines)
        
    print(f"Successfully generated '{OUTPUT_M3U}' with {matched_count} verified network channels.")

if __name__ == "__main__":
    main()
