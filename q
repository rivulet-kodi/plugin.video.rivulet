[0;1;32m●[0m stremio-server.service - Stremio streaming server (pure-Go implementation)
     Loaded: loaded (]8;;file://unknown/usr/lib/systemd/user/stremio-server.service\/usr/lib/systemd/user/stremio-server.service]8;;\; [0;1;32menabled[0m; preset: [0;1;32menabled[0m)
     Active: [0;1;32mactive (running)[0m since Fri 2026-08-21 15:38:55 CEST; 48min ago
 Invocation: b2d2114427774f219e23192ef97c68cb
       Docs: ]8;;https://github.com/M0Rf30/stremio-server-go\https://github.com/M0Rf30/stremio-server-go]8;;\
   Main PID: 172369 (stremio-server)
      Tasks: 18[0;38:5:245m (limit: 37993)[0m
     Memory: 48.3M (peak: 71M)
        CPU: 2.127s
     CGroup: /user.slice/user-1000.slice/user@1000.service/app.slice/stremio-server.service
             └─[0;38:5:245m172369 /usr/bin/stremio-server[0m

ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.332+02:00 INFO  casting: dlna disabled
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.332+02:00 INFO  engine: idle torrent removal enabled timeout=5m0s
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.332+02:00 INFO  engine: disk piece cache path=/home/gianluca/.stremio-server
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.449+02:00 INFO  media: HLS transcode using hardware encoder encoder=h264_vaapi device=/dev/dri/renderD128
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.449+02:00 INFO  http: listening version=4.21.0 addr=http://127.0.0.1:11470 app_path=/home/gianluca/.stremio-server
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.449+02:00 INFO  https: using persisted cert path=/home/gianluca/.stremio-server/https-cert.pem
ago 21 15:38:56 unknown stremio-server[172369]: 2026-08-21T15:38:56.449+02:00 INFO  https: listening version=4.21.0 addr=https://127.0.0.1:12470 app_path=/home/gianluca/.stremio-server
ago 21 15:38:57 unknown stremio-server[172369]: 2026-08-21T15:38:57.173+02:00 INFO  Unsolicited response received on idle HTTP channel starting with "<h1>File Not Found</h1><hr><i>uWebSockets/20 Server</i>"; err=<nil>
ago 21 15:38:58 unknown stremio-server[172369]: 2026-08-21T15:38:58.342+02:00 WARN  torrent: UPnP device at 192.168.0.62: mapping internal TCP port 11899: error: AddPortMapping: 500 Internal Server Error names="[github.com/anacrolix/torrent portfwd.go:24]"
ago 21 15:38:58 unknown stremio-server[172369]: 2026-08-21T15:38:58.351+02:00 WARN  torrent: UPnP device at 192.168.0.62: mapping internal UDP port 11899: error: AddPortMapping: 500 Internal Server Error names="[github.com/anacrolix/torrent portfwd.go:24]"
