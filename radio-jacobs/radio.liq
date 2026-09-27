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
    # --mark-played es rápido (solo actualiza estado); corre en background
    # para no trabar el hilo de audio en caso de lock contention.
    ignore(process.run(
      "sh -c " ^ process.quote(
        "./venv/bin/python scripts/ia_gestor.py --mark-played " ^
        process.quote(fname) ^ " >/dev/null 2>&1 &"
      )
    ))
    # El gestor completo puede tardar ~37 min descargando un lote de 120 pistas.
    # Se lanza con sh -c '... &' para que la shell haga el fork real y
    # process.run retorne inmediatamente sin bloquear el stream.
    ignore(process.run(
      "sh -c " ^ process.quote(
        "./venv/bin/python scripts/ia_gestor.py >> logs/gestor.log 2>&1 &"
      )
    ))
  end
end

# synchronous=false: handle_track corre en un hilo separado y no detiene
# la fuente de audio mientras escribe archivos o espera el fork del gestor.
play_src.on_track(synchronous=false, handle_track)

def env_or(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else list.hd(l) end
end

def env_int(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else string.to_int(default=d, list.hd(l)) end
end

def env_float(k, d) =
  l = process.read.lines("sh -c 'echo -n \"$" ^ k ^ "\"'")
  if list.length(l) == 0 then d else string.to_float(default=d, list.hd(l)) end
end

# Gate de oyentes: el stream sigue vivo siempre, pero la playlist solo
# avanza si hay alguien conectado. Sin oyentes se emite silencio, para no
# gastar canciones ni marcarlas como escuchadas. IDLE_POLL=0 lo desactiva.
mount     = "/jacobs"
idle_poll = env_float("IDLE_POLL", 5.0)
idle_hits = env_int("IDLE_HITS", 3)
idle_log  = env_int("IDLE_LOG_EVERY", 60)
music     = mksafe(play_src)
silence   = blank(id="idle_silence")
idle      = ref(true)
idle_seen = ref(0)
idle_tick = ref(0)

def listener_count() =
  l = process.read.lines(
        "./venv/bin/python scripts/listeners.py " ^ process.quote(mount)
      )
  if list.length(l) == 0 then 0 else string.to_int(default=0, list.hd(l)) end
end

def watch_listeners() =
  n = listener_count()

  # Latido: una linea con la hora en logs/listeners.log cada IDLE_LOG_EVERY
  # sondeos, para tener historial de cuanta gente escucha.
  t = idle_tick() + 1
  if t >= idle_log then
    idle_tick := 0
    ignore(process.run(
      "./venv/bin/python scripts/listeners.py --log " ^ process.quote(mount) ^
      " >/dev/null 2>&1"
    ))
  else
    idle_tick := t
  end

  if n > 0 then
    idle_seen := 0
    if idle() then
      idle := false
      log.important(label="idle", "hay " ^ string.of_int(n) ^ " oyente(s): reanudado")
    end
  else
    c = idle_seen() + 1
    idle_seen := c
    if c >= idle_hits then
      if not idle() then
        idle := true
        log.important(label="idle", "sin oyentes: pausado")
      end
    else
      log.important(
        label="idle",
        "sin oyentes " ^ string.of_int(c) ^ "/" ^ string.of_int(idle_hits)
      )
    end
  end
  idle_poll
end

stream = source.dynamic(
  id         = "listener_gate",
  infallible = true,
  fun () -> if idle() then silence else music end
)

if idle_poll > 0.0 then
  thread.run.recurrent(fast=false, delay=idle_poll, watch_listeners)
end

output.icecast(
  mount       = "/jacobs",
  host        = "localhost",
  port        = 8000,
  password    = env_or("ICE_PASSWORD", ""),
  name        = "Jacobs Collection",
  description = "Aadam Jacobs Collection - archivo de sellos en vivo (1985-2023)",
  genre       = "indie",
  public      = true,
  %mp3(bitrate = 192, samplerate = 44100, stereo = true),
  stream
)
