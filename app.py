#!/usr/bin/env python3
"""Agenda de trabajos para empresa de instalación de cortinas."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "data.json"
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
MAX_BYTES = 20 * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cortinas-agenda-cambia-esta-clave")
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.jinja_env.cache = {}

SEED_USERS = [
    {"usuario": "dueno", "password": "dueno123", "nombre": "Dueño", "rol": "dueno"},
    {"usuario": "juan", "password": "juan123", "nombre": "Juan", "rol": "instalador"},
]

STATUSES = ["nueva", "asignada", "cita", "en_curso", "finalizada"]
FASES = ["medidas", "instalacion"]
FOTO_MOMENTOS = ["general", "medidas", "inicio", "fin"]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def empty_db() -> dict:
    return {"usuarios": [dict(u) for u in SEED_USERS], "trabajos": [], "alertas": []}


def load_db() -> dict:
    if not DATA_FILE.exists():
        db = empty_db()
        save_db(db)
        return db
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_db()
    data.setdefault("trabajos", [])
    data.setdefault("alertas", [])
    if not data.get("usuarios"):
        data["usuarios"] = [dict(u) for u in SEED_USERS]
    else:
        known = {u["usuario"] for u in data["usuarios"]}
        if "dueno" not in known:
            data["usuarios"].insert(0, dict(SEED_USERS[0]))
    for t in data["trabajos"]:
        t.setdefault("fase", "medidas")
        t.setdefault("fotos", [])
        t.setdefault("mensajes", [])
        t.setdefault("relacionado_id", "")
        t.setdefault("origen_id", "")
        t.setdefault("asignado_a", "")
        t.setdefault("asignado_nombre", "")
        t.setdefault("cliente_final", "")
        t.setdefault("archivos", [])
    return data


def guardar_adjunto(file, trabajo: dict, user: dict, momento: str = "general") -> dict:
    if not file or not file.filename:
        raise ValueError("Archivo vacío")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("Usa una foto (JPG/PNG) o un PDF")
    name = f"{trabajo['id']}_{secrets.token_hex(6)}{ext}"
    dest = UPLOAD_DIR / secure_filename(name)
    file.save(dest)
    item = {
        "id": secrets.token_hex(6),
        "archivo": dest.name,
        "url": f"/uploads/{dest.name}",
        "nombre": Path(file.filename).name,
        "tipo": "pdf" if ext == ".pdf" else "foto",
        "momento": momento if momento in FOTO_MOMENTOS else "general",
        "caption": "",
        "autor": user["nombre"],
        "fecha": now_iso(),
    }
    trabajo.setdefault("archivos", []).append(item)
    if item["tipo"] == "foto":
        trabajo.setdefault("fotos", []).append(item)
    return item


def save_db(db: dict) -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


def find_trabajo(db: dict, trabajo_id: str):
    return next((t for t in db["trabajos"] if t["id"] == trabajo_id), None)


def find_user(db: dict, usuario: str):
    usuario = (usuario or "").strip().lower()
    return next((u for u in db["usuarios"] if u["usuario"] == usuario), None)


def instaladores(db: dict) -> list:
    return [u for u in db["usuarios"] if u.get("rol") == "instalador"]


def public_user(u: dict) -> dict:
    return {"usuario": u["usuario"], "nombre": u["nombre"], "rol": u["rol"]}


def add_alerta(db: dict, *, para: str, texto: str, trabajo_id: str, tipo: str) -> None:
    db["alertas"].insert(
        0,
        {
            "id": secrets.token_hex(6),
            "para": para,
            "texto": texto,
            "trabajo_id": trabajo_id,
            "tipo": tipo,
            "leida": False,
            "fecha": now_iso(),
        },
    )
    db["alertas"] = db["alertas"][:300]


def alertas_de(db: dict, user: dict) -> list:
    out = []
    for a in db["alertas"]:
        para = a.get("para")
        if user["rol"] == "dueno" and para in ("dueno", user["usuario"]):
            out.append(a)
        elif user["rol"] == "instalador" and para == user["usuario"]:
            out.append(a)
    return out


def add_msg(trabajo: dict, user: dict, texto: str) -> None:
    trabajo["mensajes"].append(
        {
            "id": secrets.token_hex(4),
            "autor": user["nombre"],
            "rol": user["rol"],
            "texto": texto,
            "fecha": now_iso(),
        }
    )


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "usuario" not in session:
            if request.path.startswith("/api/") or request.path.startswith("/uploads/"):
                return jsonify({"error": "No autenticado"}), 401
            return redirect("/")
        return fn(*args, **kwargs)

    return wrapper


def current_user() -> dict:
    db = load_db()
    username = session.get("usuario")
    info = find_user(db, username) or {}
    return {
        "usuario": username,
        "nombre": info.get("nombre", username),
        "rol": info.get("rol"),
    }


def slug_usuario(nombre: str) -> str:
    s = (nombre or "").strip().lower()
    s = s.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "instalador"


def nuevo_trabajo(user: dict, body: dict, fase: str = "medidas") -> dict:
    return {
        "id": secrets.token_hex(8),
        "fase": fase if fase in FASES else "medidas",
        "cliente": (body.get("cliente") or "").strip(),
        "cliente_final": (body.get("cliente_final") or "").strip(),
        "telefono": (body.get("telefono") or "").strip(),
        "direccion": (body.get("direccion") or "").strip(),
        "tipo": (body.get("tipo") or "").strip(),
        "medidas": (body.get("medidas") or "").strip(),
        "notas_iniciales": (body.get("notas_iniciales") or "").strip(),
        "estado": "nueva",
        "cita_fecha": (body.get("cita_fecha") or "").strip(),
        "cita_hora": (body.get("cita_hora") or "").strip(),
        "cita_nota": "",
        "creado": now_iso(),
        "actualizado": now_iso(),
        "creado_por": user["nombre"],
        "relacionado_id": (body.get("relacionado_id") or "").strip(),
        "origen_id": (body.get("origen_id") or "").strip(),
        "asignado_a": (body.get("asignado_a") or "").strip().lower(),
        "asignado_nombre": (body.get("asignado_nombre") or "").strip(),
        "mensajes": [],
        "fotos": [],
        "archivos": [],
    }


def crear_instalacion_desde(medidas: dict, user: dict) -> dict:
    inst = nuevo_trabajo(
        user,
        {
            "cliente": medidas.get("cliente"),
            "cliente_final": medidas.get("cliente_final"),
            "telefono": medidas.get("telefono"),
            "direccion": medidas.get("direccion"),
            "tipo": medidas.get("tipo"),
            "medidas": medidas.get("medidas"),
            "notas_iniciales": "Instalación creada al terminar la toma de medidas.",
            "origen_id": medidas["id"],
            "relacionado_id": medidas["id"],
            "asignado_a": medidas.get("asignado_a") or "",
            "asignado_nombre": medidas.get("asignado_nombre") or "",
        },
        fase="instalacion",
    )
    if inst["notas_iniciales"]:
        add_msg(inst, user, inst["notas_iniciales"])
    return inst


def asignar(trabajo: dict, inst: dict, user: dict, db: dict, fase_txt: str) -> None:
    trabajo["asignado_a"] = inst["usuario"]
    trabajo["asignado_nombre"] = inst["nombre"]
    if trabajo["estado"] == "nueva":
        trabajo["estado"] = "asignada"
    add_msg(trabajo, user, f"{fase_txt.capitalize()} enviada a {inst['nombre']}.")
    add_alerta(
        db,
        para=inst["usuario"],
        texto=f"Tienes un trabajo nuevo: {trabajo['cliente']} ({fase_txt}).",
        trabajo_id=trabajo["id"],
        tipo="asignada",
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/uploads/<path:filename>")
@login_required
def uploads(filename: str):
    return send_from_directory(UPLOAD_DIR, Path(filename).name)


@app.get("/api/login-opciones")
def api_login_opciones():
    db = load_db()
    return jsonify(
        {
            "usuarios": [
                {"usuario": u["usuario"], "nombre": u["nombre"], "rol": u["rol"]}
                for u in db["usuarios"]
            ]
        }
    )


@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    db = load_db()
    usuario = (body.get("usuario") or "").strip().lower()
    password = body.get("password") or ""
    user = find_user(db, usuario)
    if not user or user["password"] != password:
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    session["usuario"] = user["usuario"]
    return jsonify({"ok": True, "user": public_user(user)})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/red")
@login_required
def api_red():
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    port = request.host.split(":")[-1] if ":" in request.host else "5000"
    enlaces = [f"http://{ip}:{port}" for ip in ips]
    return jsonify({"ips": ips, "enlaces": enlaces, "puerto": port})


@app.get("/api/me")
def api_me():
    if "usuario" not in session:
        return jsonify({"user": None})
    db = load_db()
    return jsonify({"user": current_user(), "instaladores": [public_user(u) for u in instaladores(db)]})


@app.get("/api/usuarios")
@login_required
def api_usuarios():
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño"}), 403
    db = load_db()
    return jsonify({"usuarios": [public_user(u) for u in db["usuarios"]]})


@app.post("/api/usuarios")
@login_required
def api_crear_usuario():
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño puede añadir instaladores"}), 403
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "").strip()
    password = (body.get("password") or "").strip()
    if not nombre:
        return jsonify({"error": "Pon el nombre del instalador"}), 400
    if len(password) < 4:
        return jsonify({"error": "La contraseña debe tener al menos 4 caracteres"}), 400
    db = load_db()
    base = slug_usuario(nombre)
    usuario = base
    n = 2
    while find_user(db, usuario):
        usuario = f"{base}{n}"
        n += 1
    nuevo = {"usuario": usuario, "password": password, "nombre": nombre, "rol": "instalador"}
    db["usuarios"].append(nuevo)
    save_db(db)
    return jsonify({"usuario": {**public_user(nuevo), "password": password}})


@app.patch("/api/usuarios/<usuario>")
@login_required
def api_editar_usuario(usuario: str):
    yo = current_user()
    if yo["rol"] != "dueno" and yo["usuario"] != usuario:
        return jsonify({"error": "No puedes cambiar esta cuenta"}), 403
    body = request.get_json(silent=True) or {}
    db = load_db()
    u = find_user(db, usuario)
    if not u:
        return jsonify({"error": "No existe"}), 404
    if yo["rol"] != "dueno" and u["usuario"] != yo["usuario"]:
        return jsonify({"error": "No puedes cambiar esta cuenta"}), 403
    nombre = (body.get("nombre") or "").strip()
    password = (body.get("password") or "").strip()
    if nombre:
        if u["rol"] == "dueno" or yo["rol"] == "dueno":
            u["nombre"] = nombre
    if password:
        if len(password) < 4:
            return jsonify({"error": "La contraseña debe tener al menos 4 caracteres"}), 400
        u["password"] = password
    save_db(db)
    return jsonify({"usuario": public_user(u)})


@app.delete("/api/usuarios/<usuario>")
@login_required
def api_borrar_usuario(usuario: str):
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño"}), 403
    db = load_db()
    u = find_user(db, usuario)
    if not u:
        return jsonify({"error": "No existe"}), 404
    if u["rol"] == "dueno":
        return jsonify({"error": "No se puede borrar al dueño"}), 400
    db["usuarios"] = [x for x in db["usuarios"] if x["usuario"] != u["usuario"]]
    save_db(db)
    return jsonify({"ok": True})


@app.get("/api/trabajos")
@login_required
def api_listar():
    db = load_db()
    user = current_user()
    trabajos = db["trabajos"]
    if user["rol"] == "instalador":
        trabajos = [t for t in trabajos if t.get("asignado_a") == user["usuario"] and t.get("estado") != "nueva"]
    trabajos = sorted(trabajos, key=lambda t: (t.get("cita_fecha") or "9999", t.get("cita_hora") or "", t.get("creado") or ""))
    return jsonify(
        {
            "trabajos": trabajos,
            "alertas": alertas_de(db, user),
            "user": user,
            "instaladores": [public_user(u) for u in instaladores(db)],
        }
    )


@app.post("/api/trabajos")
@login_required
def api_crear():
    user = current_user()
    if user["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño puede crear trabajos"}), 403
    if request.files or request.form:
        body = request.form.to_dict()
        files = request.files.getlist("archivos") or request.files.getlist("archivo")
    else:
        body = request.get_json(silent=True) or {}
        files = []
    if not (body.get("cliente") or "").strip():
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400
    db = load_db()
    inst = find_user(db, body.get("asignado_a") or "")
    if inst and inst["rol"] == "instalador":
        body["asignado_a"] = inst["usuario"]
        body["asignado_nombre"] = inst["nombre"]
    trabajo = nuevo_trabajo(user, body, fase=body.get("fase") or "medidas")
    if trabajo["notas_iniciales"]:
        add_msg(trabajo, user, trabajo["notas_iniciales"])
    for f in files:
        if f and f.filename:
            try:
                guardar_adjunto(f, trabajo, user)
            except ValueError as err:
                return jsonify({"error": str(err)}), 400
    if inst and inst["rol"] == "instalador":
        asignar(trabajo, inst, user, db, "toma de medidas" if trabajo["fase"] == "medidas" else "instalación")
    db["trabajos"].append(trabajo)
    save_db(db)
    return jsonify({"trabajo": trabajo})


@app.patch("/api/trabajos/<trabajo_id>")
@login_required
def api_actualizar(trabajo_id: str):
    user = current_user()
    body = request.get_json(silent=True) or {}
    db = load_db()
    trabajo = find_trabajo(db, trabajo_id)
    if not trabajo:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    if user["rol"] == "instalador" and trabajo.get("asignado_a") != user["usuario"]:
        return jsonify({"error": "Este trabajo no es tuyo"}), 403

    fase_txt = "toma de medidas" if trabajo.get("fase") == "medidas" else "instalación"
    instalacion_creada = None

    if user["rol"] == "dueno" and body.get("asignado_a"):
        inst = find_user(db, body.get("asignado_a"))
        if not inst or inst["rol"] != "instalador":
            return jsonify({"error": "Instalador no válido"}), 400
        asignar(trabajo, inst, user, db, fase_txt)

    if "estado" in body:
        nuevo = body["estado"]
        if nuevo not in STATUSES:
            return jsonify({"error": "Estado no válido"}), 400
        anterior = trabajo["estado"]
        trabajo["estado"] = nuevo
        if nuevo == "en_curso" and anterior != "en_curso":
            add_msg(trabajo, user, f"Empezado ({fase_txt}).")
            add_alerta(
                db,
                para="dueno",
                texto=f"{user['nombre']} ha empezado la {fase_txt} de {trabajo['cliente']}.",
                trabajo_id=trabajo["id"],
                tipo="empezada",
            )
        if nuevo == "finalizada" and anterior != "finalizada":
            add_msg(trabajo, user, f"Finalizado ({fase_txt}).")
            add_alerta(
                db,
                para="dueno",
                texto=f"{user['nombre']} ha terminado la {fase_txt} de {trabajo['cliente']}.",
                trabajo_id=trabajo["id"],
                tipo="finalizada",
            )
            if trabajo.get("fase") == "medidas" and not trabajo.get("relacionado_id"):
                inst_job = crear_instalacion_desde(trabajo, user)
                trabajo["relacionado_id"] = inst_job["id"]
                inst_job["relacionado_id"] = trabajo["id"]
                db["trabajos"].append(inst_job)
                instalacion_creada = inst_job
                add_msg(trabajo, user, "Se ha creado la instalación de este cliente.")
                add_alerta(
                    db,
                    para="dueno",
                    texto=f"Medidas de {trabajo['cliente']} listas. Falta enviar la instalación.",
                    trabajo_id=inst_job["id"],
                    tipo="instalacion_lista",
                )

    for field in ("cita_fecha", "cita_hora", "cita_nota", "cliente", "cliente_final", "telefono", "direccion", "tipo", "medidas"):
        if field in body and user["rol"] == "dueno":
            trabajo[field] = (body.get(field) or "").strip()
        elif field in body and field in ("cita_fecha", "cita_hora", "cita_nota"):
            trabajo[field] = (body.get(field) or "").strip()

    if body.get("concertar_cita"):
        if not (body.get("cita_fecha") or trabajo.get("cita_fecha")):
            return jsonify({"error": "Indica el día de la cita"}), 400
        trabajo["cita_fecha"] = (body.get("cita_fecha") or trabajo.get("cita_fecha") or "").strip()
        trabajo["cita_hora"] = (body.get("cita_hora") or trabajo.get("cita_hora") or "").strip()
        trabajo["cita_nota"] = (body.get("cita_nota") or trabajo.get("cita_nota") or "").strip()
        if trabajo["estado"] in ("nueva", "asignada"):
            trabajo["estado"] = "cita"
        cuando = f"{trabajo['cita_fecha']} {trabajo['cita_hora']}".strip()
        extra = f" — {trabajo['cita_nota']}" if trabajo["cita_nota"] else ""
        add_msg(trabajo, user, f"Cita: {cuando}{extra}")
        add_alerta(
            db,
            para="dueno",
            texto=f"{user['nombre']} ha quedado con {trabajo['cliente']} el {cuando} ({fase_txt}).",
            trabajo_id=trabajo["id"],
            tipo="cita",
        )
        if trabajo.get("asignado_a") and user["rol"] == "dueno":
            add_alerta(
                db,
                para=trabajo["asignado_a"],
                texto=f"Cita con {trabajo['cliente']} el {cuando} ({fase_txt}).",
                trabajo_id=trabajo["id"],
                tipo="cita",
            )

    trabajo["actualizado"] = now_iso()
    save_db(db)
    return jsonify({"trabajo": trabajo, "instalacion_creada": instalacion_creada})


@app.post("/api/trabajos/<trabajo_id>/mensajes")
@login_required
def api_mensaje(trabajo_id: str):
    user = current_user()
    body = request.get_json(silent=True) or {}
    texto = (body.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "Escribe una nota"}), 400
    db = load_db()
    trabajo = find_trabajo(db, trabajo_id)
    if not trabajo:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    add_msg(trabajo, user, texto)
    trabajo["actualizado"] = now_iso()
    save_db(db)
    return jsonify({"trabajo": trabajo})


@app.post("/api/trabajos/<trabajo_id>/fotos")
@login_required
def api_foto(trabajo_id: str):
    user = current_user()
    db = load_db()
    trabajo = find_trabajo(db, trabajo_id)
    if not trabajo:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    file = request.files.get("foto") or request.files.get("archivo")
    if not file or not file.filename:
        return jsonify({"error": "No hay archivo"}), 400
    momento = (request.form.get("momento") or "general").strip()
    try:
        item = guardar_adjunto(file, trabajo, user, momento=momento)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    etiqueta = "PDF" if item["tipo"] == "pdf" else "Foto"
    add_msg(trabajo, user, f"{etiqueta} añadido.")
    if user["rol"] == "instalador":
        add_alerta(
            db,
            para="dueno",
            texto=f"{user['nombre']} ha subido un archivo de {trabajo['cliente']}.",
            trabajo_id=trabajo["id"],
            tipo="foto",
        )
    trabajo["actualizado"] = now_iso()
    save_db(db)
    return jsonify({"archivo": item, "trabajo": trabajo})


@app.delete("/api/trabajos/<trabajo_id>/fotos/<foto_id>")
@login_required
def api_borrar_foto(trabajo_id: str, foto_id: str):
    user = current_user()
    db = load_db()
    trabajo = find_trabajo(db, trabajo_id)
    if not trabajo:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    foto = next((f for f in trabajo["fotos"] if f["id"] == foto_id), None)
    if not foto:
        return jsonify({"error": "Foto no encontrada"}), 404
    trabajo["fotos"] = [f for f in trabajo["fotos"] if f["id"] != foto_id]
    path = UPLOAD_DIR / Path(foto.get("archivo", "")).name
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    trabajo["actualizado"] = now_iso()
    save_db(db)
    return jsonify({"ok": True, "trabajo": trabajo})


@app.post("/api/alertas/leer")
@login_required
def api_leer_alertas():
    user = current_user()
    body = request.get_json(silent=True) or {}
    alerta_id = body.get("id")
    db = load_db()
    for a in alertas_de(db, user):
        if alerta_id and a["id"] != alerta_id:
            continue
        a["leida"] = True
    save_db(db)
    return jsonify({"ok": True})


@app.delete("/api/trabajos/<trabajo_id>")
@login_required
def api_borrar(trabajo_id: str):
    user = current_user()
    if user["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño puede borrar"}), 403
    db = load_db()
    trabajo = find_trabajo(db, trabajo_id)
    if not trabajo:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    for foto in trabajo.get("fotos", []):
        path = UPLOAD_DIR / Path(foto.get("archivo", "")).name
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    db["trabajos"] = [t for t in db["trabajos"] if t["id"] != trabajo_id]
    save_db(db)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
