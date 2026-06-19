## Novus Tools

Tools to use Novus IPTV over multicast without the STB.

* `probe_channels.py` - use ffprobe to scan the multicast subnet for streams and extract their program number
* `generate_playlist.py` - generate a static playlist based on the above data and the EPG
* `epg_service.py` - run a live service to transform the EPG into XMLTV
