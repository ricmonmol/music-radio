set("log.stdout", true)
set("log.level", 3)

radio_dir = "/home/ricardo/Descargas/radio/radio"
queue_file = radio_dir ^ "/queue.m3u"
played_file = radio_dir ^ "/logs/played.txt"
nowplaying_file = radio_dir ^ "/logs/nowplaying.txt"

play_src = playlist(mode="normal", reload_mode="watch", queue_file)

def write_nowplaying(m) =
  title = if m["title"] != "" then m["title"] else "?" end
  artist = if m["artist"] != "" then m["artist"] else "?" end
  fname = if m["filename"] != "" then m["filename"] else "?" end
  file.write(data=(fname ^ "|" ^ artist ^ " — " ^ title), nowplaying_file)
end

def log_track(m) =
  fname = if m["filename"] != "" then m["filename"] else "?" end
  title = if m["title"] != "" then m["title"] else "?" end
  artist = if m["artist"] != "" then m["artist"] else "?" end
  line = fname ^ "|" ^ artist ^ " — " ^ title ^ "\n"
  file.write(data=line, append=true, played_file)
  write_nowplaying(m)
end

queue = source.on_track(play_src, log_track)

stream = mksafe(queue)

output.icecast(
  mount = "/radio",
  host = "localhost",
  port = 8000,
  password = "hackmeuma",
  name = "Radio algorítmica",
  description = "Radio basada en clima musical",
  genre = "indie",
  public = true,
  %mp3(bitrate = 192, samplerate = 44100, stereo = true),
  stream
)