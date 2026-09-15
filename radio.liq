set("log.stdout", true)
set("log.level", 3)

queue_file      = "queue.m3u"
played_file     = "logs/played.txt"
nowplaying_file = "logs/nowplaying.txt"

play_src = playlist(mode="normal", reload_mode="watch", queue_file)

last_fname = ref("")

def on_track(m) =
  fname  = if m["filename"] != "" then m["filename"] else "?" end
  title  = if m["title"]    != "" then m["title"]    else "?" end
  artist = if m["artist"]   != "" then m["artist"]   else "?" end
  if fname != last_fname() then
    last_fname := fname
    # Registrar la canción que acaba de empezar
    file.write(data=(fname ^ "|" ^ artist ^ " — " ^ title), nowplaying_file)
    file.write(data=(fname ^ "|" ^ artist ^ " — " ^ title ^ "\n"), append=true, played_file)
    # Chequear si hay que descargar más (gestor sale rápido si la cola está bien)
    ignore(process.run("bash -c './venv/bin/python scripts/gestor.py >> logs/gestor.log 2>&1 &'"))
  end
end

play_src.on_track(synchronous=false, on_track)

stream = mksafe(play_src)

def env_or(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else list.hd(l) end
end

output.icecast(
  mount       = "/radio",
  host        = "localhost",
  port        = 8000,
  password    = env_or("ICE_PASSWORD", "hackmeuma"),
  name        = "Radio algorítmica",
  description = "Radio basada en clima musical",
  genre       = "indie",
  public      = true,
  %mp3(bitrate = 192, samplerate = 44100, stereo = true),
  stream
)