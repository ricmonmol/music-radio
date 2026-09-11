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

# Regenerar la cola automáticamente cada N canciones distintas reproducidas.
# Liquidsoap levanta el nuevo queue.m3u solo (reload_mode="watch").
REGEN_EVERY = 10

last_played_fname = ref("")
track_count = ref(0)

def log_track(m) =
  fname = if m["filename"] != "" then m["filename"] else "?" end
  title = if m["title"] != "" then m["title"] else "?" end
  artist = if m["artist"] != "" then m["artist"] else "?" end
  # on_track puede dispararse varias veces por el mismo track; ignorar duplicado.
  if fname == last_played_fname() then
    ()
  else
    last_played_fname := fname
    line = fname ^ "|" ^ artist ^ " — " ^ title ^ "\n"
    file.write(data=line, append=true, played_file)
    write_nowplaying(m)
    track_count := !track_count + 1
    if !track_count mod REGEN_EVERY == 0 then
      log("selector: regenerando cola (track #{!track_count})...")
      ignore(process.run("bash -c './venv/bin/python scripts/selector.py --clima clima.json >> logs/select.log 2>&1 &'"))
    end
  end
end

play_src.on_track(synchronous=false, log_track)

stream = mksafe(play_src)

def env_or(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
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