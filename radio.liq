set("log.stdout", true)
set("log.level", 3)

queue_online_file  = "queue.m3u"
queue_offline_file = "queue.offline.m3u"
played_file        = "logs/played.txt"
nowplaying_file    = "logs/nowplaying.txt"

def online_done() =
  log.info(label="queue_online", "cola online agotada")
end

def offline_done() =
  log.info(label="queue_offline", "cola offline agotada")
end

online = playlist(
  id           = "queue_online",
  mode         = "normal",
  reload_mode  = "watch",
  loop         = false,
  max_fail     = 5,
  on_done      = online_done,
  queue_online_file
)

offline = playlist(
  id           = "queue_offline",
  mode         = "normal",
  reload_mode  = "watch",
  loop         = false,
  max_fail     = 5,
  on_done      = offline_done,
  queue_offline_file
)

play_src = fallback(track_sensitive=true, [online, offline])
last_fname = ref("")

def handle_track(m) =
  fname  = if m["filename"] != "" then m["filename"] else "?" end
  title  = if m["title"]    != "" then m["title"]    else "?" end
  artist = if m["artist"]   != "" then m["artist"]   else "?" end
  if fname != last_fname() then
    last_fname := fname
    file.write(data=(fname ^ "|" ^ artist ^ " — " ^ title), nowplaying_file)
    file.write(data=(fname ^ "|" ^ artist ^ " — " ^ title ^ "\n"), append=true, played_file)
    ignore(process.run(
      "./venv/bin/python scripts/gestor.py --mark-played " ^ process.quote(fname) ^
      " >/dev/null 2>&1"
    ))
    ignore(process.run(
      "./venv/bin/python scripts/gestor.py >> logs/gestor.log 2>&1 &"
    ))
  end
end

play_src.on_track(synchronous=true, handle_track)
stream = mksafe(play_src)

def env_or(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else list.hd(l) end
end

output.icecast(
  mount       = "/radio",
  host        = "localhost",
  port        = 8000,
  password    = env_or("ICE_PASSWORD", ""),
  name        = "Radio algorítmica",
  description = "Radio basada en clima musical",
  genre       = "indie",
  public      = true,
  %mp3(bitrate = 192, samplerate = 44100, stereo = true),
  stream
)
