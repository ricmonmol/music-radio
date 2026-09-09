set("log.stdout", true)
set("log.level", 3)

queue_file = "queue.m3u"
played_file = "logs/played.txt"
nowplaying_file = "logs/nowplaying.txt"

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

def env_or(k, d) =
  l = get_process_lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else list.hd(l) end
end

output.icecast(
  mount = "/radio",
  host = "localhost",
  port = 8000,
  password = env_or("ICE_PASSWORD", "hackmeuma"),
  name = "Radio algorítmica",
  description = "Radio basada en clima musical",
  genre = "indie",
  public = true,
  %mp3(bitrate = 192, samplerate = 44100, stereo = true),
  stream
)