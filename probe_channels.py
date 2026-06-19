#!/usr/bin/env python3
import subprocess
import re
import csv
import sys

# Configuration
SUBNET_PREFIX = "225.0.1"
PORT = "1234"
OUTPUT_CSV = "multicast_map.csv"
TIMEOUT_SECONDS = 4  # Time to wait for ffprobe to find a stream packet

def probe_ip(ip, port):
    """
    Spawns ffprobe to capture stream metadata and extracts the Program number.
    """
    url = f"rtp://@{ip}:{port}"
    print(f"Probing {url}... ", end="", flush=True)
    
    cmd = [
        "ffprobe", 
        "-v", "error", 
        "-show_entries", "program=program_num", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        url
    ]
    
    try:
        # Run ffprobe; it exits automatically if it reads valid program headers
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        output = result.stdout.strip()
        
        # Fallback regex parsing if stdout is empty but stderr contains the program context
        if not output:
            error_output = result.stderr
            match = re.search(r"Program\s+(\d+)", error_output)
            if match:
                output = match.group(1)
                
        if output and output.isdigit():
            print(f"FOUND Program {output}")
            return int(output)
        else:
            print("No program info found.")
            return None
            
    except subprocess.TimeoutExpired:
        print("Timeout (No stream active).")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def main():
    print(f"Starting multicast scan on {SUBNET_PREFIX}.1 to .254...")
    found_channels = []
    
    try:
        for last_octet in range(1, 255):
            ip = f"{SUBNET_PREFIX}.{last_octet}"
            program_num = probe_ip(ip, PORT)
            
            if program_num is not None:
                found_channels.append({
                    "ip": ip,
                    "port": PORT,
                    "program_number": program_num
                })
    except KeyboardInterrupt:
        print("\nScan interrupted by user.")
        
    if found_channels:
        with open(OUTPUT_CSV, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ip", "port", "program_number"])
            writer.writeheader()
            writer.writerows(found_channels)
        print(f"\nScan complete! Saved {len(found_channels)} mappings to {OUTPUT_CSV}")
    else:
        print("\nScan completed. No channels were discovered.")

if __name__ == "__main__":
    main()
