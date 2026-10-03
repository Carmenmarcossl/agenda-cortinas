#!/usr/bin/env python3
"""Agenda de trabajos para empresa de instalación de cortinas."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import smtplib
import socket
import zipfile
from email.message import EmailMessage
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, session
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
# En Render el código se borra en cada deploy. Los datos tienen que ir
# al disco persistente (variable DATA_DIR, normalmente /var/data).
DATA_DIR = Path(os.environ.get("DATA_DIR") or ( "/var/data" if Path("/var/data").is_dir() else APP_DIR ))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
MAX_BYTES = 60 * 1024 * 1024

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

STATUSES = ["nueva", "asignada", "cita", "en_curso", "incidencia", "finalizada", "facturado"]
FASES = ["medidas", "instalacion", "reparto"]
FOTO_MOMENTOS = ["general", "medidas", "inicio", "fin", "incidencia"]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


MAIL_INSTALADORES = {
    "juan": "graujuan12@hotmail.es",
    "jose": "jose@carmenmarcossl.es",
}


def correo_instalador(inst: dict | None) -> str:
    if not inst:
        return ""
    propio = (inst.get("email") or "").strip()
    if propio and "@" in propio:
        return propio
    clave = (inst.get("usuario") or "").strip().lower()
    if clave in MAIL_INSTALADORES:
        return MAIL_INSTALADORES[clave]
    nombre = (inst.get("nombre") or "").strip().lower()
    if "juan" in nombre:
        return MAIL_INSTALADORES["juan"]
    if "jose" in nombre or "josé" in nombre:
        return MAIL_INSTALADORES["jose"]
    return ""


def enviar_correo_cliente(trabajo: dict, que: str, motivo: str = "terminada") -> str:
    destinos_cli = []
    for k in ("email", "email2"):
        e = (trabajo.get(k) or "").strip()
        if e and "@" in e and e.lower() not in [x.lower() for x in destinos_cli]:
            destinos_cli.append(e)
    if not destinos_cli:
        return "sin correo de cliente"
    destino = destinos_cli[0]
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASS") or "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT") or "587")
    origen = (os.environ.get("MAIL_FROM") or user).strip()
    if not user or not password or not origen:
        return "falta configurar SMTP_USER y SMTP_PASS en Render"
    sitio = ", ".join(x for x in [trabajo.get("direccion"), trabajo.get("cp"), trabajo.get("localidad")] if x)
    tienda = trabajo.get("cliente") or ""
    final = trabajo.get("cliente_final") or ""
    quien = tienda + ((" — " + final) if final else "")
    msg = EmailMessage()
    if motivo == "cita":
        cf = trabajo.get("cita_fecha") or ""
        if len(cf) >= 10 and cf[4] == "-":
            cf = cf[8:10] + "/" + cf[5:7] + "/" + cf[0:4]
        cuando = f"{cf} {trabajo.get('cita_hora') or ''}".strip()
        msg["Subject"] = f"Cita {que} — {quien or 'Agenda Cortinas'}"
        cuerpo = (
            f"Hola,\n\n"
            f"Hemos quedado para la {que}.\n"
            f"{('Tienda: ' + tienda + chr(10)) if tienda else ''}"
            f"{('Cliente final: ' + final + chr(10)) if final else ''}"
            f"{('el ' + cuando + chr(10)) if cuando else ''}"
            f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
            f"{('Nota: ' + (trabajo.get('cita_nota') or '') + chr(10)) if trabajo.get('cita_nota') else ''}"
            f"\nUn saludo.\nAgenda Cortinas\n"
        )
    else:
        nota = (trabajo.get("incidencia_nota") or "").strip()
        que_paso = f"Qué ha ocurrido:\n{nota}\n" if nota else ""
        if motivo == "incidencia":
            msg["Subject"] = f"Incidencia — {quien or 'Agenda Cortinas'}"
            cuerpo = (
                f"Hola,\n\n"
                f"Hay una incidencia en la {que}.\n"
                f"{('Tienda: ' + tienda + chr(10)) if tienda else ''}"
                f"{('Cliente final: ' + final + chr(10)) if final else ''}"
                f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
                f"{que_paso}"
                f"\nVolveremos a citar para terminar el trabajo.\n\n"
                f"Un saludo.\nAgenda Cortinas\n"
            )
        elif motivo == "incidencia_cita":
            cf = trabajo.get("cita_fecha") or ""
            if len(cf) >= 10 and cf[4] == "-":
                cf = cf[8:10] + "/" + cf[5:7] + "/" + cf[0:4]
            cuando = f"{cf} {trabajo.get('cita_hora') or ''}".strip()
            msg["Subject"] = f"Incidencia citada — {quien or 'Agenda Cortinas'}"
            cuerpo = (
                f"Hola,\n\n"
                f"La incidencia ya está citada.\n"
                f"{('Tienda: ' + tienda + chr(10)) if tienda else ''}"
                f"{('Cliente final: ' + final + chr(10)) if final else ''}"
                f"{('Día: ' + cuando + chr(10)) if cuando else ''}"
                f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
                f"{que_paso}"
                f"\nUn saludo.\nAgenda Cortinas\n"
            )
        elif motivo == "incidencia_fin":
            msg["Subject"] = f"Incidencia terminada — {quien or 'Agenda Cortinas'}"
            cuerpo = (
                f"Hola,\n\n"
                f"La incidencia está terminada.\n"
                f"{('Tienda: ' + tienda + chr(10)) if tienda else ''}"
                f"{('Cliente final: ' + final + chr(10)) if final else ''}"
                f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
                f"{que_paso}"
                f"\nUn saludo.\nAgenda Cortinas\n"
            )
        else:
            msg["Subject"] = f"{que.capitalize()} terminada — {quien or 'Agenda Cortinas'}"
            cuerpo = (
                f"Hola,\n\n"
                f"La {que} ya está terminada.\n"
                f"{('Tienda: ' + tienda + chr(10)) if tienda else ''}"
                f"{('Cliente final: ' + final + chr(10)) if final else ''}"
                f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
                f"\nUn saludo.\nAgenda Cortinas\n"
            )
    msg["From"] = origen
    msg["To"] = destino
    copia = (os.environ.get("MAIL_COPY") or "victor@carmenmarcossl.es").strip()
    destinos = list(destinos_cli)
    ccs = []
    if copia and copia.lower() not in [x.lower() for x in destinos]:
        ccs.append(copia)
        destinos.append(copia)
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg.set_content(cuerpo)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg, to_addrs=destinos)
        extra = (" y copia a " + copia) if copia and copia.lower() not in [x.lower() for x in destinos_cli] else ""
        return "enviado a " + " y ".join(destinos_cli) + extra
    except Exception as err:
        return "no se pudo enviar: " + str(err)[:160]


def enviar_correo_recordatorio(trabajo: dict, inst: dict, destino: str) -> str:
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASS") or "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.ionos.es")
    port = int(os.environ.get("SMTP_PORT") or "587")
    origen = (os.environ.get("MAIL_FROM") or user).strip()
    if not user or not password or not origen:
        return "falta SMTP"
    if not destino or "@" not in destino:
        return "sin correo de instalador"
    fase = "toma de medidas" if trabajo.get("fase") == "medidas" else "instalación"
    sitio = ", ".join(x for x in [trabajo.get("direccion"), trabajo.get("localidad")] if x)
    msg = EmailMessage()
    msg["Subject"] = f"Pendiente de citar — {trabajo.get('cliente') or 'trabajo'} ({fase})"
    msg["From"] = origen
    msg["To"] = destino
    msg.set_content(
        f"Hola {inst.get('nombre') or ''},\n\n"
        f"Tienes una {fase} SIN CITAR:\n"
        f"Tienda: {trabajo.get('cliente') or ''}\n"
        f"{('Cliente final: ' + (trabajo.get('cliente_final') or '') + chr(10)) if trabajo.get('cliente_final') else ''}"
        f"{('Dirección: ' + sitio + chr(10)) if sitio else ''}"
        f"Teléfono: {trabajo.get('telefono') or '—'}\n\n"
        f"Llama y pon la cita en la agenda. Este aviso se repetirá cada día hasta que esté citada.\n\n"
        f"Agenda Cortinas\nhttps://agenda-cortinas.onrender.com\n"
    )
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return "ok"
    except Exception as err:
        return str(err)[:160]


def procesar_recordatorios(db: dict) -> int:
    hoy = datetime.now().strftime("%Y-%m-%d")
    enviados = 0
    usuarios = {u["usuario"]: u for u in db.get("usuarios") or []}
    for t in db.get("trabajos") or []:
        if t.get("estado") != "asignada":
            continue
        if not t.get("asignado_a"):
            continue
        inst = usuarios.get(t.get("asignado_a") or "")
        destino = correo_instalador(inst)
        if not destino:
            continue
        asignado_el = (t.get("asignado_el") or t.get("actualizado") or t.get("creado") or "")[:10]
        if asignado_el >= hoy:
            continue
        if (t.get("ultimo_recordatorio") or "")[:10] == hoy:
            continue
        aviso = enviar_correo_recordatorio(t, inst or {}, destino)
        t["ultimo_recordatorio"] = now_iso()
        if aviso == "ok":
            enviados += 1
            add_msg(t, inst or {"nombre": "Agenda", "rol": "dueno"}, f"Recordatorio de cita enviado a {destino}.")
    if enviados:
        save_db(db)
    return enviados


def empty_db() -> dict:
    return {"usuarios": [dict(u) for u in SEED_USERS], "trabajos": [], "alertas": [], "push": [], "clientes": []}


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
    data.setdefault("push", [])
    data.setdefault("clientes", [])
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
        t.setdefault("localidad", "")
        t.setdefault("cp", "")
        t.setdefault("incidencia_nota", "")
        t.setdefault("email", "")
        t.setdefault("email_final", "")
        t.setdefault("email2", "")
        t.setdefault("email_final2", "")
        t.setdefault("telefono2", "")
        t.setdefault("telefono_final", "")
        t.setdefault("telefono_final2", "")
        t.setdefault("tareas", [])
        t.setdefault("rieles", "")
        t.setdefault("rieles_incidencia", "")
        t.setdefault("asignado_el", "")
        t.setdefault("ultimo_recordatorio", "")
        t.setdefault("rieles", "")
        t.setdefault("tareas", [])
    for u in data["usuarios"]:
        u.setdefault("email", "")
        clave = (u.get("usuario") or "").lower()
        if not u["email"] and clave in MAIL_INSTALADORES:
            u["email"] = MAIL_INSTALADORES[clave]
        nom = (u.get("nombre") or "").lower()
        if not u["email"]:
            if "juan" in nom:
                u["email"] = MAIL_INSTALADORES["juan"]
            elif "jose" in nom or "josé" in nom:
                u["email"] = MAIL_INSTALADORES["jose"]
    copia_automatica()
    return data


def aligerar_pdf(path: Path) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return
    try:
        original = path.stat().st_size
        if original < 300 * 1024:
            return
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page in reader.pages:
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)
        try:
            writer.compress_identical_objects()
        except Exception:
            pass
        tmp = path.with_suffix(".min.pdf")
        with tmp.open("wb") as f:
            writer.write(f)
        if tmp.stat().st_size and tmp.stat().st_size < original:
            tmp.replace(path)
        elif tmp.exists():
            tmp.unlink()
    except Exception:
        pass


def guardar_adjunto(file, trabajo: dict, user: dict, momento: str = "general") -> dict:
    if not file or not file.filename:
        raise ValueError("Archivo vacío")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("Usa una foto (JPG/PNG) o un PDF")
    name = f"{trabajo['id']}_{secrets.token_hex(6)}{ext}"
    dest = UPLOAD_DIR / secure_filename(name)
    file.save(dest)
    if ext == ".pdf":
        aligerar_pdf(dest)
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


def copia_automatica() -> None:
    if not DATA_FILE.exists():
        return
    semana = datetime.now().strftime("%Y-W%W")
    dest = BACKUP_DIR / f"data-{semana}.json"
    if dest.exists():
        return
    try:
        shutil.copy2(DATA_FILE, dest)
        viejas = sorted(BACKUP_DIR.glob("data-*.json"))
        for antigua in viejas[:-8]:
            try:
                antigua.unlink()
            except OSError:
                pass
    except OSError:
        pass


def save_db(db: dict) -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)
    copia_automatica()


def find_trabajo(db: dict, trabajo_id: str):
    return next((t for t in db["trabajos"] if t["id"] == trabajo_id), None)


def find_user(db: dict, usuario: str):
    usuario = (usuario or "").strip().lower()
    return next((u for u in db["usuarios"] if u["usuario"] == usuario), None)


def instaladores(db: dict) -> list:
    return [u for u in db["usuarios"] if u.get("rol") == "instalador"]


def public_user(u: dict) -> dict:
    return {"usuario": u["usuario"], "nombre": u["nombre"], "rol": u["rol"], "email": u.get("email") or ""}


def ficha_aviso(trabajo: dict) -> str:
    tienda = (trabajo.get("cliente") or "").strip() or "un trabajo"
    final = (trabajo.get("cliente_final") or "").strip()
    return f"{tienda} ({final})" if final else tienda


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
    enviar_push(db, para=para, texto=texto, trabajo_id=trabajo_id)


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


def guardar_cliente(db: dict, nombre: str, email: str = "") -> dict | None:
    nombre = (nombre or "").strip()
    email = (email or "").strip()
    if not nombre:
        return None
    lista = db.setdefault("clientes", [])
    clave = nombre.lower()
    for c in lista:
        if (c.get("nombre") or "").strip().lower() == clave:
            if email:
                c["email"] = email
            return c
    nuevo = {"id": secrets.token_hex(6), "nombre": nombre, "email": email}
    lista.append(nuevo)
    lista.sort(key=lambda x: (x.get("nombre") or "").lower())
    return nuevo


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
        "email": (body.get("email") or "").strip(),
        "email2": (body.get("email2") or "").strip(),
        "email_final": (body.get("email_final") or "").strip(),
        "email_final2": (body.get("email_final2") or "").strip(),
        "telefono": (body.get("telefono") or "").strip(),
        "telefono2": (body.get("telefono2") or "").strip(),
        "telefono_final": (body.get("telefono_final") or "").strip(),
        "telefono_final2": (body.get("telefono_final2") or "").strip(),
        "direccion": (body.get("direccion") or "").strip(),
        "localidad": (body.get("localidad") or "").strip(),
        "cp": (body.get("cp") or "").strip(),
        "tipo": (body.get("tipo") or "").strip(),
        "medidas": (body.get("medidas") or "").strip(),
        "notas_iniciales": (body.get("notas_iniciales") or "").strip(),
        "estado": "nueva",
        "cita_fecha": (body.get("cita_fecha") or "").strip(),
        "cita_hora": (body.get("cita_hora") or "").strip(),
        "cita_nota": "",
        "tareas": [x.strip() for x in (body.get("tareas") or []) if str(x).strip()],
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
            "email": medidas.get("email"),
            "email2": medidas.get("email2"),
            "email_final": medidas.get("email_final"),
            "email_final2": medidas.get("email_final2"),
            "telefono": medidas.get("telefono"),
            "telefono2": medidas.get("telefono2"),
            "telefono_final": medidas.get("telefono_final"),
            "telefono_final2": medidas.get("telefono_final2"),
            "direccion": medidas.get("direccion"),
            "localidad": medidas.get("localidad"),
            "cp": medidas.get("cp"),
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
    trabajo["asignado_el"] = now_iso()
    trabajo["ultimo_recordatorio"] = ""
    if trabajo["estado"] == "nueva":
        trabajo["estado"] = "asignada"
    add_msg(trabajo, user, f"{fase_txt.capitalize()} enviada a {inst['nombre']}.")
    add_alerta(
        db,
        para=inst["usuario"],
        texto=f"Tienes un trabajo nuevo: {ficha_aviso(trabajo)} ({fase_txt}).",
        trabajo_id=trabajo["id"],
        tipo="asignada",
    )


VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "PVALtyk9-4X3H84uihVUHfXvuQEKAlZumrC_4vsmS5c")
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC", "BJXXfXBD9fcpRfHqBE9ImJeqg8Tix4CLo1unlUB0RT9nHgp7dsLWLWmcPfzVVaiM1hGEeWROrZWAT10-ZWHsJSE")
VAPID_MAIL = os.environ.get("VAPID_MAIL", "mailto:agenda@cortinas.local")


def enviar_push(db: dict, *, para: str, texto: str, trabajo_id: str = "") -> None:
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return
    destinos = []
    for s in db.get("push") or []:
        u = s.get("usuario")
        if para == "dueno":
            user = find_user(db, u) or {}
            if user.get("rol") == "dueno" or u == "dueno":
                destinos.append(s)
        elif u == para:
            destinos.append(s)
    noleidas = {}
    for a in db.get("alertas") or []:
        if a.get("leida"):
            continue
        noleidas[a.get("para")] = noleidas.get(a.get("para"), 0) + 1
    for s in destinos:
        badge = noleidas.get(s.get("usuario"), 0)
        if para == "dueno" and s.get("usuario") == "dueno":
            badge = noleidas.get("dueno", 0)
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": s.get("keys") or {}},
                data=json.dumps({"titulo": "Agenda Cortinas", "texto": texto, "trabajo_id": trabajo_id, "badge": badge}, ensure_ascii=False),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_MAIL},
            )
        except Exception:
            pass


@app.route("/")
def index():
    return render_template("index.html")


LOGO_PNG = "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rFIlQ4zKqsYnFojAcalQJF8kakiR0BA7jQT/4A1vuvZVr0EnaSWxxt77wP3f+dmPn163JknTQhTNO2HOW9/05tnTZ3KSGkUUwIxLcemVV7zivofud7XNvvodoaJmXLbx+97+R5df/DJJM5dkJrZJlt5195133b98y9atSZo2NTdNmzrt3DPPWjx3PgmgY2ElUOHklIWL/+yP/+TT/++zm3dtw8jmpZa54bGCqqTupRdc9KbXXysskbVPrHr6H7/0r8+sXeOLqq+78fr3vfOPzj3tDK5myExquJqNau984+uu/dTnPp2xIB1Sd52CIpGk2aIT5n3w3X/UWiqLyI7du/7tK1+65/77HIqotLe2ven1115z+dWaOhWNyaiT2NKbr33DQ488vHugDwmBGuoNQsXiCy1GvRrIXVXCHiFw9CAABRTAaprmaWrF3MAd9t8cfizFhht+UVWNjZ17wmzSvCq23lq0Y8cOyWcRPXul6y39/OpDxL4xDJBEybebIKJoBOaP/vDtZy49vdpfEQSwcN2PfnDXg8ujckkidMirt2342v9+Y+PWTR9+9/tiIhQBVUskjs8585wbbrqpu9LnB5QDkQJoRD1J5W8+++l5J8w55cST5s+bN37suKZSyVUyEjFkfPqvt7cPHVxx6RUL5syrViqxjQDARobF+aw3AYjI1i1bTGxXPvH4F/7jy6cuOmnuzJljR41GJJekILkrDgH19/ZMHDvujW9586TJkzOXbd+9c/Xq1Zu3bKlWqsViYfr06bPnzGkplbNKCoTq50OyxGpffuHFd9617J1vfusrL7sCgXbt2bVqzerVa9ekSdrU1DRpwsSFc+c1NzW7qs/miyFiVQK69KWX3H7vXRVxaogao/qaj0EnAGB98xve8Norr9GEQSSK4gcefuifv/TF9ds2mULEqLBn+xOrnly16umPf/jP2oplFTFEyWD17DPOWrrktt/ec0dULAioieNNO7d/6p/+8ZylZ5y0aPGECRNaSmWupN6hlBSNwWr3wIRRY97y1rdOnTI1qaabtmx6as3qjZs3V9Okqalpwthx82bPGTdqDGQZsgIzERkkTtLzzjjrp7+4ceOObfme5zmtxLmBPwCqtjQ3G1/BicgiAwMDgJRwdue995y59Ix69lVV1fGE0WMXzpu39dYtNo5G5EYQQEQNGef4zFNO62xpTauJ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"
ICON_PNG = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAABuGUlEQVR42u39Z58kV5LeiT6PneMRKUqhUEBDtEbLmaH63UsuZ8m75Dt+AH7dfbWXHO5eDndmOaJ7WitoXSIzI/yYPffFcY+MVEAWUAVkVdq/qx1VWV6RkR7u9hyzY4L/+j//JyRJkiTXD8tLkCRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkT4Kal+AaIhpAwIEgBAAqBkAAEZAAAkABIDnJAgbNCchLiBTnf0yRYEyLiXhab5pBkKIAAaKIMIECWSVzCghAgoGGCFIwQbIQALDE6esAKm+HJD2A5BpZfwpVqCLFEB1wQ1A0VopQABEKCMUKKFgQBlgzc5JSiSAccEAEKANMKE/rjmKIAclEyEQGFf2dh6rMYOEEC8mABBNIwgwgpCgAFQaIBK1Mb9hE5i2RpAeQXBeovuyVTKS6SYQIGlgcASs0QHKXQjRDuLuHQaVaGSAohMlN6EtoQcRTNKYCQMDEIPviHVApgzyah4ygSVGKmRSh/jNEhMhaK0WEJocBIEAIBJQuQJICkFwnASgSJUEiREImMAT3xkpAEU4zVoa7RZhgQEgID9BBsRARkEjTtEh/mvaUFKjZdAMAoeIh0sxMViWX3N2LlVqAFuERJlZzqLnMTNO/hRAmSZOSJEkKQHJdFIByAsAUGhdMMFgpRqGRKFAb14G2qIsiWchMRoUCNNBEeiAsQIRU5AaFODsET9wB6Mt26wGnaRMAFlS1wVU83AqMtBDaChoLrJIrcnQ3lMWwjNZma983L8KEIKfXTJIUgOQ6MNk9RI+DhCFgDEBe6bWN8rVFA4zrI9JNgGopw85i2YAxIJBWBe8bBiCmo57WrhIBgUFq4wEQQJUwegQ1FBYF1odoo8kKShmWqosAIjC6k/29qb8C+36FpNwJTlIAkmtj/eGmgIIEJTDAAAtRPZYY63i4U+LO7b29nf3WnBbhfnQ03j8aD1ajbKnFrmxoPtv9rXX6U3VbqAAMRLfYFOjdKWExg9bWVjeKvXT37o3d3Uf3H314/9HRwYq7e7Us3MVu8Ckq83+SJAXgegoAEdysqvuaukAs0EC3g0ffuXfzL9741os39xWyWgXBuGrtT+999Nu33n37wfrBWDUYYXO6aM8GfZoZZYy+YwFEX7Abou/nEipWCxXjapf+599+/cfffW2n2Hg0/vy3b/7Pd975yI9oA2kMqId+qOjuyuRYJEkKQHKN2Fg/QDbl8kSzdvStF2/95Z/94JUbg43rxlYtBKhwbXH7my+8fvvm//63//hgtaLVAgubYikABIqknlYdgAiflIsgKBCiwl1lMPm4w/jOvRf+xXe/tW9j0QGG4V+88c0Pjx4+/PhBi2YsBkR3Hlh6QqgdR4OSJAUguQ62f1rxWv8NOeX4WLTdiu+8eu/e3lDXB4PWC0HrNY1tVKlqKju7+z94+aX33v7oSEFO0Z/+OsJmA+DJa4CO37oEUCKCQiGt9O8Zu4XfuHXzhSJbPzQdEYvl8s7Lt/d+/+Dhw1ih7BA02VRHNmXDSpkHmlxjshDsWn7qwSLWwBAo0QuiQPnOUF+798LSvPhhxVi4NqyHGId2tCOv7XDf/Luv3NuhIFHsaZUCrKfofEW3k7bDNiQlF9oAvXx7dxnrnbZa+GoR41Lx4q1bC2OEc6r7CjC6RGkqBMjlf5IeQHLtNAA9f78AQIigopLFQhHFpBgDZKlwVYLmBQ3rw52yBEJyY3BKqXnqa+jjSP0cuKJggCRJTphCaDtDZVsVNCLQWluvKi2kvj8x7SELIkSUE1vW6QYkKQDJNYHSJh1ysn/qNtXbiAojJWCoDSSKmY3rVbGCsbkXFtuE+vnVveXjzYbNmw4UhwfNhiI0qCFUAMnAwZ0ASZpZqO9Th6HvJJC9AjpvhiQFILlWuCEQRhQgAIeJ6maxmBkZrsoFrK5bDGXR1Fa+WtQy1IENZSho/ZUCkD29BnDHC3SzafFPkzahG7PSgqqMKAKbWrGKYLAaBqtLmsMqZOGx8SNMEA0MioQitwGSFIDk+hBUz4qPaf9WQQThchAFRhUa2wja4LR1o+3suKLSAnIxjvuIYm7UE0/PHzAc94GYF+4S4QHQQDS5GxxioY8MK80Fqy5rrt4wAj67LFNH0XQAkhSA5PrR8z45BcFFqogUAgjJ5KS5JEUpJaIZo3BQW7tCAF0ExNZzKoUyGVXqKZlUTYW/PWmH83frb7kXI3eDXqeu1KTg0Jqu4qBglcHeqoLdn9jeuuDl3sPjv+un9eldgb1r8fhW+rouRZICkDz+g2sBABYFEMwRAGiy3m7fEEA4YMUUo4XXUsYjmNXe8bOCBskaZJBJ1KQn8dQEgAH2ptC9c5GJgoySgrLKgQqqRMiMjiiDBdaLgiVKDTkgyslAndKBIJnwOP2gH7N33FPsi/e1C8DUkkmbzqynr8zJQusUgBSA5Ios/6fHtv//ghQe9rj45CYIsuOMSeFE5Idb69Gn+Jxrs/DdlB103ZkGEoDdMm5ahU4Zn9P2NjYxq9lQiYrtH+Fy5vxxGkg8LRvNK2BSp/tm63KcujKZYpUCkCRPwRfARZp12ujo8xbkT3kf4HkWgMvYdqYApAAkyZM0fNo0Mjrh05y73uz5/tOvM9qgL/YGrrzZ/Zo+mNMfE8+4m0kKQJI8GYPDzyw71gWugKYhxldsSf9sCkAQdp4en1j784pduSQFIHkWMeFL1hx/+WbVcWUuxhVRobOb/hR6n5Cr7LEkKQBJknxZKb1AU2eh5maWc5IC8EzEFuacQH3p4lBekF6oK1x2KkkKwBSilZCLIgmSPcWGfIKD3x/vtXT+G6YxYm5LKhnL8fnqb3j6PjYVkfXpkv2HwkVuxbnvLa7GUlZ6oh/Dl/AAeKE2aGo2cnUcliQF4DIW8Aq+VF7tyVpz6u0D9D+QRphMvbnRsdXuZwQkISIiEBFT/F/Ri8fECIoXDAQ+/70Zn4CSfenrcxWs/4UL+2ntT+uzIkgAcs8wUArAlTZDX9lDde5TfUUe6av7GR0vKaW+tp//BylCoRAVETHVik2FyQwUsAAxOQKUzACTevkAH2cifEDdgRBODBSbXZDTb/rUadPx0h81p/JnnJleJs4rcGn6NnwSE9kepybunEQqkmYFQKi/L/UrY2n9UwCuftjnufx2X94iSOqxITzpd/5FQkCcMgu5UdMpIMJjZtOvQjOamQlmFuFbwYlZS3S+1TtXp42UqZebHR9B49RWYzNt+DOOlz1NIAgDA9o6zkGVqf5uE2OXpp8Z9K3j2agVgQDs5FHHn0hXzhPfcvN7bT4FRZzZFDdIIe8b9mIubFIArrEAZAjocy+FXXD2eR+SfdbHp/lD3OjARjIU8ikEZJrSQPsAnG6keEExtM7fcwj5ORp5HJKfGxUFwDipE92mb1pan/yrC05Tdzp68+1NC+7SU+01n0MAKNabIMXJo878INzK34kTuTzaek3E5miE1Gc/9B5Q/eWDpz+pEHqb1e6kTJlAQnZbTQG40ubp4gf+SizBn0pEBVvFOphWpZfOj+em1ofbWfUXxTcuktiIS0fDtBUC0mblLgAmhBBQSOFydwux9wqCekJi3yMICYJj/httWkFcFtMkRKf6sZ1SuOOYjY6Ppy76dieFs8fJIPef9cRHtgkATY6CpL46j+YiKW0fceb86XjSq5iOvbmPcOp1ID/jo4imY0VSmdQWJrlIGUn2qyVCMGYUKAXg6hlXAYA3IUzqK5hJEKb1Cy/uF3BOdPcSp3WP+/jUY0vB8/53usT13G969ltdFFnY2K0AaNNz29e0Zu2IFYxe37P5npt/vBGK4LiytUMFKhbdJmqzAL+8iNnlHbIpVOOgC0UwyogwggEKNcYBbYCMhbOxKeRADfBlqAqIJliAJhS5KA/ZhQ4gL3rDpy4qLvz3OvsJTJfo7IdzXrif59XQsrswm7cx/7TzMub0tzTYGQHovcDP3LfdkfncW4hAoFgQm4SrzT5KoZmHRvcWPvoYEQ6xLmQVk6/BrUE82myqsC8mtN2s++QHwROLtFN9qZIUgM+hVBvHZqVADKlAFmOJxvHw5rLe2h32yrBTrQoRUrW+oiFCp4KkZ2KpZAEc03iVU7foqZCrzMrGrM6PpQwFFgzTieMkAjhWCBPDUMTov59i0oIRtD7vUFt/tTlONqY/b4QTospWKQ9N+zeWdtu8OWk2NYsDZYCMJhIh3TD922/dfdhCxbpObrd80Xmxmou6rRU+hs9jmzEAUydKI4W+WgWI2CPuLiE5Spm03dd3F/bvf/CNI8hp80dXuvfQNeXChMrzvtxfope/bo7ilqGeIzlxnonl1i106q8Mm1fbFgAev9p83HIfNi9KQGa2eaWTwf3Tiw1A5Ll7AJufRJ957FoYJxf1Nh2LhWN0X7sfHB5++uknHz08+PjowScrrIeF7e55WHMNsO5zCu5CtQIXRi6Xwwqrft9CnCZBzGuAqQ/qNLwO58ahksdbBP/r//yfrk8kWlBIxcqyFIyrMh69uFe/+407L9/cuXdjZ79wERhCkNzoxOa5O15qcPOQsxvpWQBObKZtSQXPfSBP7QdCOGOyDXwMAYBN7TBPCYCC2ojJsQBERcyL0mP7HdEMLGbkcfxhY966qZXkfSF4nq3XJg3/MpLMxyitPWOoOX2km+QaAhGUjNHfufqy1erc3PQCuXmMMNv5BvJ8p4+fE9a7nAt5UeO705adtC+/ICYuKwBQ8MSSnLNlZvQf3opHrFarB+P4wVH89sMHv/7g44/H8LrnHAwGedBLsZAYqBgsBinGuprue1kfVDGvLaatZQomFFkwxIi04ikAl4l6h6G5L8kdM394/0bRj7/12vdfvffi/mKXvqSbmglGmuhT3PP0/4DTX7zoKeYFj1AIwGShe1g5KKPh7LfriY5nvqmdFyuK6Rk5G0QyTXGA6QcgQMTZzLweZt/Oa+w218y2xIDknHt4fv2UnR+/+azNiEtYvAvsaU861MwcY99enG6F0Xj+suCpBRj5GD/1F7HVOuF3fMWGQ9qaAbfZRkKIIKN/5EYrxYbFo4YjL0dr/PLt9/729299FIr9/YBZoDj6cDeUKvJwvRqGPq+CQDHN29l2nHVKwSCqCAqm/c8Q0CWfl1IALcnh8OHLS/sXP/jOD15/ab/SfG3jCt6sGEtp0NjGRTGTICMCMvYGJ7PNmPdAp+2z7gfMC3xtWZaz+XZRWHq4draU6mnpgaBMW8epfb3mgLJEmBSE4cRR3LwaNefETMfwBiimMe/TYioUfn4St007p+Sxud92oXrOJS90vKXHeCAfqyTi3B3jTVLO1kudmPCyLUgXfDu7/Ht73DUHLr278MXX60/yBb/sG+h3f7FCWoR7RItG912ru9Fowyvf+9Ybr7/6N3988x/ee++Rc7BlQVEDCtemQ41YAMFN5HFO/wogou9TAyCi56QmKQCXp0UsavVPP35xZ/jLn3zvh9+4W+LQHzwcKms3BI51eJisFgvvQ6MwHXXime5L/PkETinYOt4lm87pXzxx3Awz3AwYERABwUMmxuaIOXI/yQAlGRgRBrpU5uOUpM/jDeXj3I5q9fhdTSt8yuwCIxvzytq2jf7GNPc/RgQvaArB8xqmXWjTz7NcXywrd6NYG2dm245/Rq1fZut+QXkjz62gIBjhogOopdenUW1E87rQ2NrLpfzHH337tbs3/vpXv3vnwYMY9qwuXLIWy2Vt0vbiwhTRZ44pbEpppbacq1SBFIDH+FFtXN2A/uUPvvPDl+/4/fd2d1iWgEItGBSskCrGKlv3NreB4+O5K7xe6BizuT+19jsnzTsY3A4QEASsEFNOxfGRmHOqT0bORZ08eauW9fil+z+1iHbOj9C3dD/P+EbEZsm/bWG3replPIAL2yKdlwf0WBa5C5VOmvvtHmXbr/blBeCK92Z4Akv6S/8gF1wKkowIhWjUVIbRBrIsyioOAezaoqxWP3lh7+6//On/9Yvf/fLdj9ZlybJoUQyF3rqjLBim+H73pDcLoenm19fq+6QAPHMRIJVxXMb459/55o++cW85PioDrMTh+rBYhbDgYllqVRz6et3awqrpqdxddkGB0bkKE49jWc4vXt1ay29djsAFq29t+TpGhIJz453N69hnRYHKV/mxSo5NufL89uauQScs2hNZ6T/3ha2Xv0ra9Fs69QJwGopZv/EiwsFWyuHow9Kqsa0PBhuW0HK5WPzo9cLxVx8fPCTdiq0BVRT1GdXz8si3I0254k8B+GJmV4Pa3YE/fv0be4w4fLTc0aOjA9sZXKWyMGxso3uzqv2dobWv9F57IpblM17kdKXSBXu42wt8M5vD/doEfzZ/9XjG4kksnD9jw+BsPOrswr+vTC//HjJe9LkKoAvmqpF099badBlJCaWWiFi1NUO0tbVWdPTqzs1/99Pvt3/67T99cJ+LRTiI0p3I4LQCAcwEYgoFGTh39bPjUGKSAnDqFmyt1Vr7b0gjpPWj73zz9RdvDHH4cLeWiPWw3FkJYgWKCYUoQ6G11kZoiEt317LHMQvBr+eKnHGJLjSyGxk4FVrZtrMXdnd4nE3geKwEzEvL20V/++X1Jtm+gYhN3DJO3VenJLnOW0vBBUoIanSTtH70ys7N//Cj7xX7w8/eu78auMZQQbiLBUT0ZhYsRiLcprQ2i8d75pLr5wFsFq09uYVqu0Uv7S8GjQVei3kUb251cJTeBaFvPwVCxwnRl1wMXdqsXJUQgp7a62ZU9jp5AZdbHlHel++UCQZJZkQUI9YHL9Xdf//975Xxd//w3od154a4GMPLUGA2joIVh8IxoABBETDD6Tq0JAXg/GjG5IQ231/ai7d3jW7WY+sGVykF0XMJFASIsZgEw9zg8Sk8Ml+7DPCpaYCy/eN1cQB0rg6c2pulQIjw7vCJpAwyeIQ1j8aIha9fr3v/8Y1v7yh+/u4Hh8NeGXa8rVegaGa11zkqJBSbE7x4JuMiSQGYYwsR3NoGlELe7tzevb1TSrRi9BagVULeu5FINnUWBgrI3gTr6ZjIvOuS50MDLvWEBI/HhNlcUNibeRQUoyRHO3p5d/c//OA7e639zfufHNLMilhGwttIq4UmCAzElPtJzfU4SQrAaSM7lzL17Ur3MOnlm7f2hBIO0sNLKWbFoxVueQAiYsCUz67HeBTwNM5Nkqtq/aeSkvOV4LgYpq//SQA1QITYC9JlAj1YzYZ6uF5Xx0u1/OWPvne0ePsf3/34wFe7N24iQjBMSzNpMwqHUg4cTgH47PjPxhsIaTksXr55ezdgQLizmDOKgWqY+jlY31wy1amF7dOpMr8SpSt8WsF6PV5jnVy+PcOrLGBrxM3FHz3Dpo4dU+eGmNrHSipsVPhahnWszOqwt/znP/3RoX7967ffXR0dDsOODTZKCk0FYVMv2+gB27lnXHItBWCO8ih6E4SZYuYhAcUoaKBuDPXOzu4CIHEwjju7e81XHk4jpyT8qTPP1LGHTyvnmFfi2eXT/kwub0SSZ1cDpuk3/JyPvngRIbooSr0ZrawEGb3YhSK5LnHkqxvDjX/90x8S/k9vv+NDbQ3QUMqg2BQEaOtZVzoB11QAOOVfyhnT/DkAvctBOEQryyaZqRx++vqdOzeXJo2uqAPD2zwiHNrULhFAEK2vWy6aWPUlief6dmUmZV8X2z+1gvj8hkTclPIGZEAEiglBYt5z661MXFqAWB8uWP/tG68eHn30d59+zL2XSgwKUOzBIGcRAojsA5EewLmLyCBQCO8V5cSO4uW9YVHgiD5CVt3K48xAcErwvC2S5Ak6cb010IxtL4O4WdBLtU+/1CiOd6v/8PUXf7N69EiriCVhNgtJHxJA5VLjCWDP+M3Xx85tXMPjH6d3eSSEGHcX5c7Nm4OVDDkkyZW3SUG6WXzzlZe//eK9um5Qk51p85SRnxQAnLgnbOuHEglIhmD4zmK4sb/bezrnR54kV9qtJ1tzI/dL+eErr+4ZaqERooKnBkFye82XXFMBOPnjGCBFrwEOmhBxc2e5W43Pefg9SZ4PBRBNBapj+/bN26/evIFY62RKXh8Klu0grrUA9IhgHA9I3fINe1fL6S6JW3s7y9Lb9ydJcpWtf0S0UkzSEvZiKd976SXGGG2t46zhmMcrJekBnEhEm3+qYlIATrWidntvuVuMkQuGJLnyjzMZ0aphiNgd47Xbt27vLADvi72Y3fznMYCRAvCFiKlXvXGaTN03f3tjN18YbyyWA8MUuXGUJFdeAKbJdObj0v2FYbh3e8+ibQ2giDkdKB/nFICey9nTkqfCFEYErY8Jabs7w97SyjShN52AJLnSuLdaq7dmRIm2P9i9/Z0abW7+A0mK3hYiPYAUgC0Z2Po9zQwEpUUti2oFYdnhPUmeGcPUZ1h7oe8PdbeUPhd7fsC1PRk4ua4CQMH6ul4U+i8A0PFIip3FsFuHorDcBE6SZ8WnpwU0WitFt5eL3VIZveELyT4vsjeESxG41h6ANg1JuOUHNEb0FoKK3cWwrMVyBzhJno1nenqUvWBdwgw3al2a8TgNSL31l8icP3HNBQDnhvUFursUJBaLWozylp1DkuRZWP6zDw4I2trU6AOwAPvz2/165W5eCsB0u2iaOrRdH9iXCxBoXAwLIyyrwJLkGTFJlAALYytyNIYzRMFO1P1aZBrol+a5mQdAgeDUJdxoEQJVYTulVDI2o+SSJLnyuHreHimEOyJ6UacDwV4CbAZRYYjeWFRznwg7uTOQW3/PrQD0uS29H0gw+lcooKladV8vpNssRWpUJg0kyVVf/ssUEFkWDLTajGV5FGNzL4LHOtxaIWAm1lCl+sjXBlod5KBkoIVTQG8Uz2mCPLNx0HPqAfRKgJj0nmD0CUQooUWgd4HI7aIkueIEUEpZe5M3GHY5jK1pXO3aeAdrcRjNVoYI1vFIEau2VrVhWMLqehytDIFg0E7OqeweQ17e51UALtKFE+S2UZI8AxpQiMJSi69WJRzm33lx987eGyOMBShyhkLDKlZa/vG+/+bDT//00YeL3T1YGX1kKWFaCxSsJ4kfT6VHZoJcJwEAQJqZ5eI/SZ6dZVu0EJ3woS7gvqDdu7mrUggnGtAM2NkZVrZ/66W7L70e/v/87bsPH5bFThjVmz4SxmnbT0TATMjtgGskAJrKSTb/SZLkGcDdC2g0ycJFDnXYXaM0kTKSAgqwUlmHRdHR6iDcDYA3mKFwyhiViYAMAmii5/L/OnkAgiSC096vcnh0kjwLHoDLaEUMDB5sdfn+o/b+o4MYFtbdA0qhMvoq2lsP3n37k08+PngUdekSQkZt14t1D4DA/PinCDz/AtDnAZxw97J9eJI8C48uipk3DwRtESwHWv7srbf+n9+92Rb7oEkaiwI0N8gP47AsCpbLUQTKwOIRhp4oKhGCQlTOjrl2HsC23EuQMhCUJM8GBSzFhQDXtPtNnwYDpiiC1gEZiw2gy2xtgTDAilWXyDKXkgmQEISVngeYpUDXRAAI9LQfdXL5nyTPxIqNcsgRIjzAunBhJY9aohbIBFhBABEEK0hH0ApF3zQFUBgpiup5P0HRMvpzDT2AJEmeKZddsqbwIPqOr4eP4WESQckERXfmnWHBYr0nRN/pI5zRW0MSvfSLlHoeSApACkCSJFfZeVcgZA4EUQUfg+tYiwiooM99EuYgTw1CJXqtL+GmsB4CCJunAhoz/T8FIEmSZ8D+yxAwIAJAgM19dA8WZwEKienvaATr3OGhZ32ERIUoQU5YLwKQld4EglkHkAKQJMmV1gCjhChmxVXUfGwMloAZGYKzjwYgEW6at3ZpYOkC0CvA+nBwGGBBZB5QCkCSJFccg6yI4SM0AIMC7kEYQEIGB6LN27trEwELmmhCkUpMghCcTwK6BuQcgRSAJEmuMDLAIEJmYggt4FGEMPjc13NK79E8PWzyAvrer9Anw1q3+3lJr40A2NzrQ5I4rRg2jmUWASTJlYdhtLWvhopQk2nV2qEHSKIJRSgBAFECQdCnfwYx5qL/6YWOX3Tz21z+P/8egGXLpyR5dgk4ECKBEKIhnCBEhVSCEK0HdnpYf9u696mR579qcoG5TJIkuUpuwOyvS2rNQ7qoj1cu6VMAkiR5LmXAHGgeConnj/RLAUgBSJLkObL7my6efbqrNxe68Wd29E0BSJLkOUZB0iDKGMIY4QLYC3tFgTqe8JWkACRJ8nz5APN/AuER0Xv5ir3md9KJvE4pABfeQb0BVP+/psLAJEmeBRdAhQwoQBcOV+s59kMCnBf/opQtflIAkiR5zgTAWAQGGcLYGkh1CTgR+pHSDUgBSJLkOXTf+w4w0EIBm/NACcD6NkBGgVIAkiR57jwA0zy/zz3W3sRj65+kACRJ8jx7ALOxtxa+dpdN433JNFgpAEmSPMcC0DM3ZCJaRGuNZLbySgFIkuRaIAkEyQi5xwnjv5UJmnx5ns920L1PbL9NsmbkuflMT/3eLl5CUgH0seAWjLlF4Paxz4g1nmwzEzz/W/dGBCajHCyQiwVwAD1C3TNStGWcdOaP81haAbBe3Dr9o5g6WKIAhOz4H1E2JzEHgWsw2UoWESBCsjVqCxZKCFARxxeUsny0UwAAwARxMvolev2IhKl8PKS5OXimDVy1Dy4ABA04YYd1opE3iAhAx1Zv6ghwqrpjs1Fo2mSIiNNs2OjDZru1BaRpPEhs8ksEQLYxvQHDcbt5iLQAei2S+pypfgRJCIEIBsC+e0kyvOeq9x+lV7MSm9RFRm9q3xPbyRCAMFPveExBZBA+qZKsTz/vs9Gf4ym3DocBIUc5wmLVUBcIuRBhAbhJBgRI5TOdAjAtB6Zn9dx2UXmPPCNL/KkDzBlfDgUKbVv508vtE6sBGPryUBAIBWBQnyo1eQCbeiInxNDJeRHblqXPGKEkQIaRYDiIvvznVG8IAgUk5gGFioEFQkiS1CUhSBKEpAinaARYDVEFAU44QgTBQkr9h+h3NtXb3ut5X8rM2hbg6AxaL/kS+g+vrU8mBSAFIHkurP8cHtle+3e/bhr9VC92+Lf/RkCAYtnyDPrvCwWgzJGXbl8pxkiJZohJWkSDAJmOBcAEJ9zQCM7uJAETJ+dFPT+9W3sgZBQBI0kETEEoorc4oxWWvrMpgWJtRZAPUHGHG8CgSf2/PjkJKNB1KIDiLMhjawhdFPQXkbXAKQDJ8/LYn7ZrmlfiPYZXtoz87Njx2BDEbAxEEQ3HwZypbmhaSc8CMMkANIRAzd89INMUn9msMOWc/uUQiB7kmZzOHimyIDxAA2mAEUEJCkSEIJK0QtK6XnEKAklQhDhiAYjyEmvCARmMMLFos9nBri66HrFvCrEex74dPLs93LL4aftTAJLnMBB0ShI0m4MyG3EdewdzTMe3hr8S3uMzsx7wuInAVvuYbv5rsMQcVmAAEALqO64QEfPOqwkWKtqaPGUMANb3KCHrMkMaodK8EUQtNKMZYR6imbGEQhJEgjSC5bCR9BprRitq6JGPeZDVHLCSWw+EPP9ZkSIisG6jA9G7QIicOgLpuQ+DpQAk1yHuw/MWfjpPD0Jsc4OAEzFgsu8gbwvAccuwvuTnZhV9ZvEs0WlnA8rafotzUNo0mhxAEGTpg6oCCIksxBQekuCobXHDYQ6EtHY/Wq+PVu1wfXBwtDpYrVbrNcDFzs7e/v7ezs6dW7f2zW6VshultiPEOqKryfFmtzglKZXn2vRNq33SiXXzoEROeaHol1zUVqpUkgKQPJurfV4uFtTDO62n7pwK+9rWeNgTZoTbJxjVs4MI2PHLz8ljU9bYJr9oegunmg/LaQEnGEaQZA2zoEU39IHVOB6t1oer1ZHjk9WDB2sdrA4Ojtbr1kb3tWLt0aQGeG93YIel3C+M3QVfvbn/xt2737q1/0LdX2ABjOo734w5pa3/7Nck+ZEAXeFgAOLx57jtJeQeQArAufcOpWAP80okTyYWJldz6dfzLU/PfQoaoCbfzPwmQJLsuTl9z5A9HVMRDvWMTKOxrxoDAMwGSQhNu8GEwmkuhRQAQCNNPdtSCgRhsGJmEANc2Y21Bpeax5GPj47WDw8PHq3WD9fro+YH6/Xher0a21rhshHmgIRQz+cnVEXKeuoppyEnAdP4cDx4dPTxH9/58N7O8s9ee+WHr37jpu2gHcLXVigLb+POYnd9tCpl0PNdByCadenlwdGqp89G5NObApBcAyJCW1GgoAEOYzXbDAsXJA+FSqkRcncpCovRyDqYgororzS5DSTHHqIPkNZfSFYEOI0GKwVWAgywBQRrobHhaDUerg+PDo4O1v7pig/XeHB0dHi0GiNCakAj1kITg3ASXISRZvJmxrC+9Vw2yqWNq9HzPQHRCnHoqxVxcBjv/dPvf/POh//su9/8/st3ij+KGKkYhuHo6OjW7s2DgwOrjGvgBrjkIdA0renOWcKlE5ACkDwvSz/2BEkzcs6eh/ViKzgiiCAhkYJkAGIUyUVZWCkmi946pnFpDClA9Rew0ucLOhF9rlSpkoJco65Q1qOvVuPh2B4eHR2tVoejf/Lw0dp1NLajNq49POSCSnHIITOzUsieoU4JZCnG0gsGZIgwkW2qBRNjqlXsbe577uicZURFQRk1rKy2JR/Z+tHDw3d//ou/ePTyv/zO6zuMXTDW650yrA6PFothjPYc3wZTNE8W0hgtDOJU/3DS1nc3KLcCUgCS52nd546pG0wBIFNBIcOECIUE0Mx6DRVZFHTJm8gAibpsgQZjKUGKbIiR5ooj92YaA0dtfHj44NHBo0dHq/sH7dEaY/O1t3XzVchdjSxlGbSgBXeimIxCsAR7RJoEiwnR+hBbo837vxIj0JWH2s5k2gp2nY7kK7gYdkWuFLZzY83lO48+OfrNn8bm/+aNb1tb7XAJRNNo12P3U4RHuDtgoU1/0C7pcMsWLykAyXO5ALQ65XdqKquSVOYmDOrRFVrAWoQRbgywB/ZDcsURFocxrMY4XK8fHR08PFodjO3IxwdHR0dtfLgeD9vYIBG0Qg5SYRBWrS5oBaRoawfm9sPkZN7BVcCjt5boHXwoHCcRYeoCUQlgVNk2Z9smDP17aPNHC7TmI1AK2dyboe7e+Pjw4d/84Z1lXfyrb7+2KG5+VBfD2EaU51kDyCJR1Oi+Wveia/ayvO7NHWdl8TlPiEoBSJ5zzi7jwh1k9B1cFBoBBOrhtJymyDW0GqO5VqG1x7r54Wo8HNcHh4eH6/HBWg9WrQXG5qNHg3vPq68lYMEdDXtgEeUKgqbSi8ycgBQNIS9mkICICIGl0Kw0DwjFrKcVCFOcqpTSC7qmIEXPHe2byJpyN4/bAG2FraffMFrEVK5AFNHDHGXYuXlwePDXv/rD3u7uj19+Yde4CK+F7XrcG+ExehMHUWRv6ZGkAJznL57suohza+WZpSNfiUHXxZdcJ80+zXr7tCmtp/9+qrRiE71p9HF0P1R9oPpobIdH66PVeDiuH63HVYtHq/XKfdViDLUp2Qbg1IGNRtoALozFYH132WSU9X1gCeqLeUCClWnRL6h5K8WsmERXSFKMS1saTBERit4TjkWK8LE7L8bYClXzuGNc/+k3bUOnBDVuap1ZDL0TXbgFK63YYCQWvH+k//LzXwa+86N7t+7VGkdHVixoF1x2nmg8+sw+zoK54N3on+oPRRzLbHKdBWDqwXu8Jzj9KaY8OfWvMWBhgIOnlp48ZZp4LCF5d30R68+AGZsUQIERTilMAsIIM7OBqhJGWrM6gi3Cm6/dx9HHiNV6XDU/Gv1wNR6u1kdHR6u1HwUPXEcezSOEHtx3ElbAEjagmkhaEYFooTbl/k7JpZDAYlMH4Z6aI0iiEYQipHCXyAIGYaSEaB6QQDPQarh7uE3DqaZ1PYmp/ovd2s/tgOaW5JvKpr5ZPBvnng40VTXT0LxBWtSBsnCoacUopZSdvY+P7v/1L36/u/zJ8t7tOmov1qSPNJaKoEUzg09xKUOfmsueg8pn7hbC5DhxDK1DrCyx8Zckc7GJQRUqF3XXWgDUF1ABEipib8c714vCApJCQlhxhvVa/+2Wk9uCoLklsXBBkWry2ViAEYSplAAkK7ChEIUr+lG0tYev2jiOh0dxELwfOGx+uF4frcfD9Xi0bkctVqEWGEOjywWwkAsWGoQq1u38mdmU9l1VCd6jIzIWxLGwT8sBzWtHm3qL9kY+04q5TPUHMb2oTd+iJyCGpvN6o7hNpfC2AZrsPrm5r7a9IeFEiRu3vwtCotHE8JhuXoJmTQ20YffmR48e/NXPftv+7Cc/evGF3Ufvm1wG0KheLiYQMtNcKkUFEcLUueiZEYCpFkSiHYWvRLGULtW9vxIjzEHVVnBuZWByvUJAU20kEdOgj5gahEncPPGXfhlkWvGX+jCqIkAnVyzDcv9o1Ccf3f/g0cNPxtVDbwcrX6+0blivY3St1Vq05t7QMzVrsEQxwMIKipE25QLRPZzS+UvG03++8FPXiWjMZ59znnN47glf/L49bk3BuSEqtyIbAQh0WPi4u7P74cHB//jFr3Z+8v0f375lR48IYmy1WItwiTZQNi+h1bNl5i7Kz9jz3FuytlgHeWaYh07WQ1PpBFxXAaDNAzuo3pd3WtFF7w6JqUVXmMK0lUv2mdHHvCm+xPqtFK7VYthdYXjv0foPb374x7c/vL9uD6SVbBSFAg6iEbEUzIy1VDMRLig23e97v2aXXNELaZ/bT6Zbfout2QMMkiJFkmxNrdrO3t479+//X//0i50/+8lrN26Xw4cLhrFZgWgMVFGMacIBQ/EshskJKIgAxtZ0Yi7QsTOXq7QUgH4fcD6WeYlQRA+W4+g+u/WfIkM9tsMT/vvJmaOpAF988UY3rkash+UfP7j/P37++/c+WbkWGnbHMjgR6A2RIRgY61CBwNI/iWDANJfH9ogNDD2q8dw/8lO2I/vAABiAQgbZ5ByGMdyF5XLvT5+u/svPf/e//fmPX9vbXz/6KHzFYsAwKSYCiJgDUjrRFu+ZEQDBRKzW63k3hSec/dygSwHYuH/cCugHCAZUwGlcB/uAEHmfGsnTN885K/5cW3xhAmUlDrdv/vz9j//qH3/59oNx2HsxYrEOqhR2ZwwN6DuWoNURUiAUDLCYVVLq2TkAiCDZWz/6c54EyLkn3WYjlNFcdYjwWqvI1dptuQuV33x4uPzl7/+3n3773u6+NbZxPUy3fBBRpnkBm0qDZ+oW6omzUAjjOOq8hKatJ9aAzA29xgIwW+wIEpsh0b1Je98xEojuUApTY5lTd5K2NvQ2d1UuMb4IThMWnxzp73771nuP1uXmi0cjzVhqla8LoqgZXAq3cCzCdsCFmQghQoiIHvbo7fyEKaVGcoDXp+h/loEIRjMWd9Vah2Udw6VhZ3fnV+9+VDD+x3/2xk3GzlAY3hOfKBlgQiPHPpdAz5gKBE0wwsbm0ednnvsjZD/oJ8GzfBEpMTY7vWIAMVl/9ZJxGTRN/SaybchTt1tkG/Z+8af3f/fBJ7FzYyx1TYbQxjXCEaOiwRvUCIHu3sLXEU3RPMYIJxDu7h4RmFt+snfveZ6vWx8oiQCDimk+WSwXC06pT7Zeje6KhjIs1rJVWfzyg0/+6me/fYDFyKVHj6313sk9AtTjZuVZXMyICMojekuQk1675fIsPYDNjdKDBXFs5Ke7X9VKBQ0Ml9kAMSJoA826fSmlWIHmaEOGgL6sHJOkPWz41Vvv23J/DWstWIZwmpXohbMsDohohoAVw2Z0V+9uQEXZWun7cQD7WjzzfSkzZygz3CsYEt2WXCAA2lSyUIcj2D++9fFO3f03b7y2V2JHKwqttZ3l7jjGKNiyqj2DVcMsvaPS0dF6mgB2tgw4l/8pAJtnBpt0h3kylBHhI8DmCpVVi4JiVgQogmStFVBrTVIpJe+DJ+W8f/Jo9fDIx2pgJayPR+wbuDEPag/ampWM4itDTPVS0/48z/l8J3/vWoR++mT6rTZBKFNsc4p3B1QqYfXwKII7f//mB8vK/9f3XjFvQ/Od3f3Do7WVxXKxPFivLpH4dmWvg6LXzWUJfwrAZ6wWNlOcMC3/ZQJpfUrfGPjo4eGhXlrUZQ1HRCjIboq6DKBHG3CtlppP67m1Bwerg9GbVZZSYPSw0Gaot0ihOE0oVPTyDcKmQX86dsBiHra+rS/X5jJOe6GY0xxCxz8+Ga7GWNS6O4Z/MB787e/fWS74z167SwQCqMuVNCgGK3B/tu7pyfTT3NE8jjd+lCOdUgDOVQBtsoAClE0RIJkVB8H61seffnA07t7a0/qgUqQRCHl4WIFZ+pJPEoeC83yuiN6oE0ZYzGPYxSiwuekPP99A6XkXZmqeR0/1ViYAfGp1acBU3xV0YJ5+rABkdWGDfXT0yV//8g8L6aff/Ib7WmioXI/jrhUa/BkzmwYZSI9Yt7a98y8ExXQHnvTlfpajPyYzGWG9ZmaT9RZkI0fRh+UnK//Zn975qEWrQ5/Q7QDNylBJymXC2V/JFwoByaqhEADDqShAIQtEBBCGVjUOOtqNwyHWoAIl+ghGbDqn2dZtGXNrhuf8I+l33aaLXE9iCDJ6SwdG0OejihkBb2siXMDOjQ9H/vdf/enXHx6sF/utVhIVgK/tGWyUIAK0Fr4eR1rv6sULbFeu3q6xB8Apd5qETQXB6B0eIdJlorHamvj12x/c3lvuv3Z3pxYGwkOIWmrvAcbzX3w7NzTDQpf8RDQMVg2EmwgUYyHojAaEhcmrUABIBjqKyN634Ez4X5sjn/cCbQImQZBN5SpTYz0RNNOmuY/6QMsIhftyZ9HaGmCjDTu331s9/K8/+xXrD7939wYOP61TOe2zd/f2pE/3aG3EsAswH8AUgM+wOcbjvIl53RhgrS6OEbTqjX/3m9/Xw/s/fOn2S3dfKBVtXIXMbOgdE3km5uBzK8c5LDm1KLGeecRNqDqbxp34MJaDDTVMBoEkZC3QirnBDSb2fF0Lm2eiTJV8cwuYEwLAreY9z7EVmJcvsCncD21WNOjjBHS8OoYgo0HwPh5Bqm4Ddm+9/fCjv/75r3d+9N3v3l5wPAIoxbxMnlrUab6w2yubuSDmKlzhacqv01rMQ6B1URRQ6atfXwEQIIrYXsNTlKb1VFSBiBC4WH5y5H/z+w/f/fjw1W/o7u1b+7v7y2FgiOHkqUU/ojejlwxjZQwDFqUUsUYr69VSrcBhWkdwWMa0VLvuSGLE3sAFXKrBaiwRwamWdwpxCOYEaeydCqitfo46LxxwDS4dQCI2be6P1U5UbF0ZIkwgWQh5awUMiJQjwDLs3nrzk0//+y9+N/zZ91+7cyOOHhQ0Y5iKRZ2K7czNREUQQum9Ikxe5ILNkvw1hsJkUBMeOlasJjO5GUMCrLv4myaglLIV6LX2AAIg5/5hPVAgm3wBH8vkFihQuXPzk9X6wSft1w/e3dv5+Mbe7qIUwulObjfvpQgXQ5WFFofAaihx58aNF2/cefXW/su7N7R+xNaktljWB+OqLHYYeRdO9mmHtiAizEslqd60H7Teow8SSsD6HEVDbGf+XLg4vgYCECcEkNSpUNjx14kiGBGl7w8TQgg2eqMVW9z4/f2H+MVv/81Pv/vanVvt0adLBUIUFDCaGUIjjpMrOXfUnmuvv25nqCqOpIfBVRkQKHAMRQ7xRMhfCGaG6LUPAc2hgwtNxmRgGlj2dlvEYWsPW/vg/qcG9XElttVnFt3jFsHFUFi0jjjytl58cH9/8fErt27+5PWXv/fC/r4BrqEMaGv30ZCVBBOFXA4LayRNvR2ZhD7BXRJPRR7ShccFvs9nWEhxExETjkeowcLKWAAtfvfex4tii7/44UuLW3bwEAwfpJAEUuFBs9mn6Pc/nRVXo2qg1/WP4b0RkIgzbcC3FnzJNReAy99Wh97CUJdDoaQIBMMoGeexIOJxvVFYi2YKGxZlsbsSD1t89O7HH3z68OhH3/zRy7f3l2V18HCxXKzXY96HcwAOVm1nMeggePyIamrpBdvU/SZfTixE+NTus194odTBQ6BZ3RmMv3nvY/zsd/+fH3//G/VG02FjY+3dN0QyaJo3nyk46SyETF9/wlWvAxjHPuKT2Exq6iHEfNZSAL6geTJjMQVaBBSECopRLTQnIB573nVRIEh1VKyDzsrlYLW9tT74Lz//Ler3f/zynYGDRl9m1uiWZSrC7nJgHPQNcrJvBfdU917BPz3BkU/yF8U2EaOtThkhrddjHSrL7uEos71fv/vJHv/4v/7ge3vD0seVWcNAFwpqH4ppCCDcLHr2fc9++PrvIgMwjqNCqBcM+E6e2L10jdanZo0I0cm5gAAqpmIspBFmKGQhy9HRet0iaODgXDoWI4c27MXerY/W+tnv3/nw4aoudhcY6JEKcHw/UTuLJXpgbQpQbLL7jyek5wV70v4tIqIsBpecZYWhDfvr4cbfv/n+f//1H+5HWe7sVxqaCwqbhgTEvPNMbFchfP3LiIDW49r7W6Pl/ZIC8ARuqwG2gC1gO1Z3rC5QzWFNNUoJK2FFVmQljLKd5bKwaO2xDgtWFpONLdZi2bv5zif33/ng49FpHAa3zEQ4DgGBO0Nlb/qAAKSYA/95lZ5YkMSE062TCjVUAxChstgZUcY6PBiW//c77/7PP77zYFVq2WezPiM7GOolVrMFIHRlWi2YWFoLaXs2xKQNWz9zVoGlADzmgxPR1NxjlPeZAcYpN/3ENSmgHEZWG4YyDGCJoLdaAeqgjWvZWx9+fOh+eLSqw5C30awAMUDLWsuUWKWQYmviwpmFXvLF4yRT9YT6KIxQ+Hp1NBRzb+txZC1HLbS7/2Cx/Ovf/vHv33r/QEvWG8WWfTRGzCoNgXCwgf71bwD0Ojhx7KPuewhxbvl1XklYysCX4hrtAUTvMNZb0QOgz8k76rMFNtFpAJRMZIjyeTQ3m4slbCiO8v6DBweO24vlGOvsTTuv3NDaeGt/zxSb8b7cemw3m3jByGv2JS6zzQOONsUDMmkwINZ1qouHlbKOQF180vz/97t3aln889fu7cHoRypce2OplYz1CPOtEN3XujkjcxjMDg6OzApoTXOhzybhL0dCpgA89n3Vp8xyewF66sbTdkqpEX1vuAgG57RnqRBFHy3W4KG0JksFI0fTzYEIoRqHakaFQbK58pQG09YaMwNCX9Lb0ryfQkSffLddLjzf1aU5sNz7cH30V7/83TAMf/byrUXzGlFR29hQhsVi8GgwJ2pcjbvYpQCiV32BUDAr7lMAnoDffGFt6XFNTI8zxqb/us151nSwFzAxrBxFHEFrw6KfknS3ybSkLWqFxCnXkJuLTvWyYCgreL7UbTzl1WpqkWycG6HrxPqGaDGUElbWQ/lwbf/Hz39r+O5fvPJCO3iwMNZa1/JDea1m3jycLFfgx+MYai706aCftVjI2ygF4NLrJZCMs1Z/c9dNN1T/T0AiNI8o76mM1vvUk6DCm4dk/Q+ZnTxf5YhaynIYtGq9q2Vvl9RTufMyPZkoCWIalDhfUKGLrbbtZS/AC4WiWFnG0t47/PSvfvG7RSk/untTbeVqYvhUDll4NaYtEvDmrbXey4h27lTjzcZACsCX4hpFYk00fMavrV7QkmmarSrSDT553ANUqGpaELUEK2hZzXpqTVFsOQzhIfVdYJ2S2uRL2kdTmMJ60frUTLtE7xOhuVBMBtCkIhR3X60DBTv774z6r//0m99+/OCwLqMWV5RickYMxb7+dIbeDLW5jy7RQsDpoa06MwYqSQG4nGt56lePSXQrv92QuHekKj0zWgQqUKCBKBbVvA5RB7dBNijonnUAGwfAoGJ16JlRc+7G+S23My30Cz6xIsLggKbB76A2AwXY1zm91tdEK+QALMkSVFQMN988aP/HL3/9mwefeC3VsHQtVKWFX5GNeVqEwts0J/SckU1p+lMAHp8gz/7q4/d6jH9TF9OHsm4MFMOgApBR6cYRdFnIAhage96Px245ORQORaYoMIPNEiDxeLwvdbbBS3LJdUyP/fQuqtsZthTKNEgARWD0Rg9ymoiArwdasPrOjT8djf/tV7/53YcfsS7lpMhSQwaVuVr75ByevjI/FnJtb+Zr6899NNP0Czx7jk6+7ukvAABc8ugbv6Q49wuP/tv+4uf+w+Sx/fXrtD69bIqDZJtGBZxqZHoyqILFiiQ5FVTQYNkJbktlQ4D2bywi1qY9ynps2tkcIAwwRti8JZC5U499haepAZtLqOO/wWaTa8qhB+E07x1+CEcTSVrj/h8+efh//vrd+tNbr+3dKOPhgBVgjCqDMBbSfQxhudjx1nt4YC7eCDAgEywYWy09rAiUcTOj1QRGMDbjjAHM/Xr7Llts/O6YtrUhxrqt1960KBU7pihBtxaGQGXUIoMEa2HZGyg9gKf1mHXbZAAMjQiTgIC5LMJCpunWJ7OtzfYCFcKi0hjdQyoS0frMhH6hSvrwX1oDtlLhA/C5h4+2Pgagh4kwzxcwCU2IgDkXsbz91v34//7Dbz9oFjt7K7VaC4A2jiBphVaKWRvH8z7l3jg0Ng2FgDDF/P05j1MSEFTYpBmx5Sig77rNPgI5pwyA1qI55KEWER6FMkZYm5ZcsjIJUq4fUgCSq+ZpMYZaKqm+dz4N78wL8/UIMnsvDp6MlrB6lEeO9+4f/te//cf3Dhv2bq98LByHgS3akYeXpWzp04q+9SFJQQUsWAR2o0+pSEV9Z6JNZ1oTGuHdXSiKEihCjaiKIaLILGgqFsWiCCUwOBfCoqEctSZaABGhwByYSlc7BSC5+gIALOtQi00lnDpnoGOPJ+f67Sv5OGQIk3oBMRGARcBpZbl/CHvzo4f/7e9++d5B+GIn6CyyUlugtQhYKXXOLY0+zkGkYJsdHQOK+i+BEkN00cPC+6QkGWVl2qc2m8NEx6MwZX1vm7KgjeKDwxE0oxWrrCZmS9AUgORZiU8g9oY6sEiCMU6WyaUr8JVbf00df7Y+B5esDkeyGG6shv0/fLr+q5/99iPn0VAO27oUG6x06x1QAG7HwXZNmwDmNMHmLd/JxwjKTc3U2McMzPl1KnNTWNvsKE8b2Zw6/ptgYarLR039xWU06926bKvoIXuJpwAkV9D2z3kiO3VYGqc8H2F7CljydbllW78NkqOHDYsVbRyWjzT84aNHf/WzX763drtxaz2qBHaLaVyRcgsneooRMZX39XB8L0EAJuPOPlHVENZN/GTVpdpP6LLRaI1qVDMFA3RiNLSiRvoqcP9gDRRJEe7abDp3FelSE8ib6klQ8xIkT1YFirQsQzWLFn3U7PaTynmQX2QV51di+QXgTJqCx7i3u390eLSo5chVhuGh4xfv3S/L4f/9vf27NizaqsplHuj5uyVUCmQKKsLgqH2gfEwFNQYoGEIwEAaQFmZTyiYFeG9YsckmZX81SWKvZEYQ5YNP739w/8BJWpEjFAaKRSfynZL0AL5uMi55TvyHAmPJsqRRoV4EdrKTF5VP8Fd2i/YFs23l3gQRe4thffhoMZhHwCizqMNh2fv7P338N799+7DsaFiOoaFWeaMHIFqf7dmD/qLalLsDkOTxjMkuFyhzgXKZRm1rNv5OOBkGMQKKnkfXCDdbwX739gcPVmuxmpkZudVKqnsThMTIWyg9gK/heaJxU5suiYSU3Wm3rhBBaUHdWO7g4GE3GeRUDbydtJ0jvb8yD0Cg9XZWUK/WVlvXYvJm88xmGZ3LMewf/vTRgPqvv/uNF5b7Pj4sVkm4ghZhYfMWcIGHqaBIEgZOy8lN0RbYIz4ioEYX+xYyel0I+iBjSWSwrEJludPc/vDh/V+8+aaGm7Ta3InStwpI28wV0nHLiyQFILliBodQgRZmRjgl9W6pfcB3dvX96iWZAEKcd17nEQJonDremhgCjLUudg7WD//v3785+vhvfvjde7uL8fDTZWXE6DZaLWOL4liyDAzXiHChNLmAym7/G0DbfOOu+hZ9hMHUniJsKvoyG2krMXaWbdj57Tvv//UvfrcqO40Vgs05ZNoqLJhHSijTCVIAkitobgR4FXcXCyNnl6DHILa6V86/zaf4K3ABNPXNpLb8gL4RA0GIafhuH9653H/k/Nu3P2wsf/nGd+/t3Dk6fIBiLaJQIqpVOAMBNdmCtQYWY8hpiuhy01NOQfaJe14khsksQBhVuj/Q3GK5jGH5icevfv/ez//wp7cfNezuOgtITKWWOvZl5j/mXZMCkFxRCVBEBfeGxfTM2lzmH8ygz1f/cWy8APXRDLJpq2YSAwgURZiAUAhUXR4Sf//WB6uj8S9/8sY3bt4zHPr6wAqpIIvDwWLD7mGwoXrdHWGlLjyahRuiKKDeTb2IVBkhGWhiCRb1OnGMsodjvPX+R7/+4OPfvP/B/TF2Xnj1aD1O2wm9/0+3/pPNPx7pMc2by084BSC5UuaG0gInPIC8Ll/b8h8bg7npITe34sG0eSVOE4Z7wXAIApsrWN48XP/vf/fz77x4e1gf3Fjg+998Zcc4xrioJOsq8Pv3Pvz9hw8OuDiKwYYBERUqcFMA8N5vkSxk6fN952/WY/lr4f7R+v0HB0eo68Wum3268moFiDL5KMfr/U0dwrz8zwSWFIDkSvoAhdwZFpwKR4+fVOFEF5vk6QtA33plsH8IPR2IAQI+C4TAIpDVQk7FANQ6hNX3W/twbG/97tHeePTGrZvfe21nqO64P8pR99fcfefhez9788OHtr+qC5aBQA0VNCoEuNGJIKtbEWNatUdM0RyJcEFWUIcR5iCsOFXU53LH7L2cmimdeRcpAF+zUz2RuWinDA5RIJi1oaLPqgVMiGCw7zmmR/D1eQMn82/LPPKoABDkCBDWApQVtIhWSinDAR3BA9UxaKKVElBz13JY284j7Iy7N7wsG0yBYl7UTCGgGd0gsKCW6O3Wo/cHnSJSDIjGAplAKwUA5q694uktok0+aH6aKQBfge980tYTU1cDKQiHeo5jVqVvYeagUXEwLHyoVY5SamCchmrCQHcGYHNDmOSpLlaIPoR5aqDAs0uXvtC2uS7PCiUGgrAq42iAuY3rIbyO0GghsQ6SRRuDWu41yeVTCInFKYlufc8ZJhipsokGmnGatw2V425Rx9VqMVeKmG3F/ud6ciLX/ykAX6MwxLShlsv/866PDIJspJEEYX3AiAADKYb1bb3IGO5X463qoj+d/uD6qBlhTr0HYNMtbgG6haZ6XpgMCsFFOi1oQFC99M+wqfgFGTAoTg7jnnMDcOaLpx+pfMSe+pItL0HyRGNADEhW1O3/NEJks6HnW8Vg+XQnSQpA8pytOqWw4sTUwlHiydGFSZKkACTPpQsQYWy0VVOLPqbKeXpwnzE38pIkBSB53pb/gFAayuEqWnPrjSC1PQU+b7kkSQFInlMBCNqaiyNHG8PAPgy294Wew0C5+k+SFIDkuaNJUeshh/cfHnqvAVNsOsDNQ0JSAJ55phZPZrm3kwJwvVa4nO2Z+iyLbHC5vfwHvQxvfnz/zQ8+5TBIQaHQOI1+3W4JnabjGbH1593hJCWZmebW6PkspAAk195YWLl/uPrHX//+k8NVXey4JGNvAj3NhgJObwknSZICkDwH9l9W3nzv/Tc//HQsC7CSJsrdpzoA2VTMOZ+elyxJUgCS54EgA3zvo/tHqFzsu3rPl97Q0QD2ooCtf5ECkCQpAMnzowE2yjTsjTKXRUhAb/K1MfYZAUqSFIDkOURAXexEKUe96YN1Y0+oTO2+xLzrkiQFIHkeYZDeG7a7q9hA0ZuTQcgkMJChnyv62WF7bOfFJ9ln/3Xu7KQAPP/r3J73BkDK+31DFLW7N3ewWu2W0rs/m6PABJq8yIE41RsyuUoKwLPqLIFkeJ/yMuV9Yt7LIUmApJ2RAWbf5hSA5DrdTFEQe0NZFDHcaN0+iCH6OctL5e13Zdc49gXsw+bsdAJSAJLruIQksLe7u1cHhBsgiTbNJOlVdJinQmYJaZKkACTP07KRCO0vlrd2F/QV4REeQEw9gnpUYAoRIDtCJEkKQPL8rP8BBnaH4d6t/Roj1CTXnPc5x4S5iQ6nACRJCkDyHGlAxMLw8p2bS4TJaQhKUBjCpjHf7CPAMwSUJCkAyfNj/QUjFuDd/eUSXuHW6wCMQEAbmy8CTAcgSVIAnjWEuRVitj88LQCAoSD8zrK+sFfNGxVG8ngcuYHKtf8z+wH3nM8pDXr6Sn8qMhk6BeBaPQhCljOdJ4+CIfYsXr69Sx8ZgVAP/4sQCIiMTAB9PlZCSQpAkhwTgQIOWL/ywu2BURTs8yABgOJkNoLIYrAkSQFIniPr3wu95AvEvdt7u7WqtULapiJURjAAMZQakCQpAMlzdT+ZyX2g7y/qrRv7am4kJdNU+XXsAeTuSZKkACTPEyQlN7Vqce/OHQMhZcp/kqQAJM8/AZhB3pZW7ty6uTNUuBuIwDwOrO8G5x7i1Rbyz/n7/PSeE2pegs96DM5rZ7j5Yj4E5xgGCVbgbUncXZb9wiN3X1aXqQ+Nh0wASnAqDOgJQlO3IB7/cfuYfBWfHSDCjAxVeQBBD8LNmwVhDCMDcECCaAtwBBswWBSDkRRcFo0CrK8vY1oYyPr6AIjzBEbzX2G7WewF2WI2dy3NbaQUgKf1MAAoU95ib2Wz6WbAiKAxEKAxCMss6Pm6EYoYbKG136v1pb360f2Vm61gJtVYl4CBgQoIDPXxADz+5+cev4ByX/4NP73Vw2Ndt6e3guHl3oALoMHI8EUAoSO2WFqzsOKCFQyUBLGMUJi52SoY5ECvpckGeWluAZABCwsaoCKA3usEN8dzVd8Y0YOGxy5lbx8yNxGZHJSuF/38JAXgSVuxE46woM2SQ1MjdJ18zHLMYb8mpEICSni7sbd39+a+PnoEh5mVaDVQoIDcXCAhC27byv6bs0c8tV2EjekJws4cKRy7execJm76nB6fpi/0Ni56tUt+03NOu7yrKtZiAY2tGQkbwAAlgowBZigt5MVQiorTa3BgFHPRDFbH4jR3Th9ogRMQqsAgCH6Gum/90YhgCFPG8KQRoijI+s8qIShTbjClACRXiginiVRrboW379zaeeuj8KBoUYoERhSFrQNlcNilK8Ie6yl/LPvba1utt7I4czzxiuedwO3vN5/2WO7LtE7Xha92yW969rRN4cVlrplJkEfIzQ6kEL2YFGVEHd3Mx3HVFgNZW3NEtdgdtKyraGhY1LUBhQKrWKQSANwRwXJ5I03BVCwU1nWOAR27iVP9pQOBPocGNRdfKQDJlYCAqw0YIJlh1Y5u3ti9tbM8PBxtWMxB4f70ApCMl28hcHmb/kX2DDjNQzx7PKE/vfPB2RNOnzaryuVMkwiLk//85Ktd8pue+yNcMt2WgKJJslJgXBsDMkOJWKzG29Q3dnde2NkdLMa1DDYMi3EcXxrK9+/s/+7Bg/tccffGWm6gwkoEewG4MQwl4tJRUloYYf2CzN3DTRQgSPNmAW2OyGb0NQUguSoUGwiG+7JWj/H2YufW7uLt+59q2BNMZpMJDRUGYMFLbQLPDaW3Yx4XHvkU940fd7F57nvE2S+Klzrtkq+2OWoTHrrMq0mFhMENMkKB9Xrh62/d2PmX3/rud+/e3DNvR4eLYsWWbfTK9Xde3N/d++b+Ox/8/Qcfvb9e23Lfog8AMgqidVeEEOGXu2I2OzPa8s9om9YrjCn6LzgJ5vZbCkByNRAAMwkmFY+9orHy5Tt7v//k04dcRVkKKComVAQEwQQ7Heu/eBvg3KX22aPmX1/WfJ4jOXa506YgjCkgQ+99NFvjrfPY/0qMvjUuGKEzr0ai/9X28bKnTZ2355NEUiHayaNEmrwaRkkxGkzj4c3w79zZ+19/8oNXduqe1mxHsFZYEQpvi6Uq47WbvPPit26+vfPffvvWh4cPynDTpk+NBppKiHqMTRE1EzDOP5wde0lTP5G6EQpRPSUpH70UgORK4AFCBfTVuuxwb/Bvv3TrVx+8//DgoFt+YZDMonWHYYpVnLb+AoyKzVGI4zHkOJEKQsTJI/su4WQ0RM1G9uRx2pOc4vVnTuN0nE6jIHI++dSraRP037waEBQI66q4vRurzc97/FcGQrD5n596NYh25vj5p3H723Frg5p25ggT0AIlYCK0CLdx/e1be//hpz94ac8W7YHGwwKRRfAQrNDbutaoiuXY/uK1l1z1//zlHw+8gbXH6ynZ1AmKQdvSwoB4RoF7rkUDApwzRTVlAYnEcUITMYW2RDB9gBSArzbSTZDQDMlcgxz777RAABhKwEeuD169tffD11745LdvPxgPUW9IBAfasB5XVgD41vNLs57X0SPip448s9snSZuJk/ORFlNGuroVQo9HnzoSc1dXTTkmJ08IBN3EHkM3IWB94U1BjK2jNkGqOWZFMJxSr4yejrOC8ZxwTwmo5xzPx+nEk1+c/ko6cdqZEzZHJxzy42HtfVeABgVhwOZYgGpFrXDAYF5Xh3dR//2PfviNgba+Dx7SwkmGCRalKEQqrLW2qhF3yB/ffenhq/Hff/e2dixIEvJGU+mr+EmTYz5af4Q2ujidAGeE9VzR+QcN0YrRzCN6FuqmH3varxSA5CpFgYiQOYM0KmqMSz/66asv3v/00c/f/CTKzqMxnHXY312vDnbrcNxRHpNBmLZZzw2xnN4qYCHFwm4U5iMkxHHOCIW+Hj95VO9qP68qT59Gk8HAOQbBzTIeU0jlxPGMbzKtWTV7KsenSLGdnblZoZtOh7P6iadO7sLAywXEjIBtF1ZNwX6QthVhselNW6iufW3yfeEHr770ys5yuXqgoQU9CiwMMMEC1WiAj3JYqQ3Dyl+y3TfuvvzLdz/6oLWoVYUFQIWZ3EEEBHBzFNgV9cRWCGjFihSa5IFGlmKjN3mrtYr0NgZkZrUUeT5zKQDJVWEyNY0G60nbWoyrb+ze/F+++0070m/ee7Dcv/0g/NOHH9+8eWN9+KheoADnbzDMsZ3+m76IP71X3I+fmVx6mdSjzxr1I80mLGaP4ExwH4ELMlR4sXZe8tTLR9QFwC+ZactRi7pcyKgIxtG3Xv1GlQwIASgQqFKjOqpolSWihayUYYjgUdtb2Ku377x69+77776nUoMl4KPkzQf2gFTg+AhQ0qmtE3UREg0MI4GQQh7VBiPkAan0KFVI3mhpwVIAkqsTH0OAEK2hGEBEhfTwwet7d/b/4qc3/ul3//DWB1W8sbtUOyBCtJ4DYySncNoc9b+oJgsnN3wvtt+63Abv4/+QceZ46o0JAEN9b2H7f+AU2Dn1F4+1Py3hktvYBhjtjKrEHGfbdGcQwFFtPT5CbWyP9ge8dOdGbYfVyhoGVQAWtYT1mi7TSEVUGIqBCsf60XKXr7yw9/O3V2uvQkVENRph4TaVU37GHkBAJXol8NQ2nEaZEUED5WouSKWUYkVQU1YBpwAkV8n6lx7ehfViIKBR691q68P7N8reX/7Z9++9ePsX77z/3sH9Dx/eL3u3Ryki1I0+raf1CYreOWjreDbZBidP6Mc5B+icRf5UjKrjmAw/62d5TLfg/H++nQM0+Th9RU2CW3vN50rS1rc7adl1rvSdsuk9zh5nvBDOMf9TAqC9UhRHsV6VOHrp9u3B1u6rMUaUWsIIWJBCVQRXlPcSLDUXgUrXIV0vLnQX7WB91MIixIX1LFAeb+rOW7unC9n6ZwgVBaQICZDJERGglTosFoNAD65DYGUR5NkQKAUguTL3kxpgjkGqAZoAxhhjKYBWCPzZ6/e+9Y0X3vrwvTc/fvDhQVsL4YqQekKo0dBTFT+rMdzmj3YmBNSrZO3zLTXn/5zvRJxZOG/+0Rkje4EtLqVcIkIzmfXyON6I2aXLp+fGDKeuUqGdvW5WzH00LkvsfvPWXl0/KhhrLa6ASg89uYXooFuApUhsIRRagTQW6sVF+1ev33XbCw4REYxAwArPBKJInhVUUqGeIcYWWHs8PFo9PFwfrNvRuB5VmhltgTKIiIiSj1wKwNew0v28eO71xHq9j1AAF4USANiKlVALrajg0Xin1Hsv3/3zl188WLUQpQgF+z4rz13H9i5DPGs+7bzasYvW6Se/viUAuvRif0o8v5QAwGz7fc3vWee+sj2WANA+V1G240U87T/IYKdjUCJYAALNMFqsS6yBRpLzdoZbwFzWiEAzsyqVBqkQbMGjoQzfurl45QevW9k3W4YCHIFwXTbeVqTB3RDO0oqtgw9GfzTqvYcHb3/y4L0Hhx88OjxsLjisBi6TgKd8TFMAHtPAa0qY0xwomB9ihqQgVXp6CAXKxbzDpkdNqkCBBLTe7xO9OIAGkooKtwgdNSpuWdGc3k2QdGyyAc+xyOd9R20Z8+Pj+bu8fKxwz0U/4aU9ADmDYbJ+nMoFNnozhYDCZCdjMpeL7Tz503q+qkVEHQAFwkMa22isogvTWAdq3sYNgChGEh4CGBJiPXCh9UPoEUKgk6p23r68zvmcilT7O5FqQbGyrPX2YK/evvujV++9/+Dg/fuP/vjBp2999PGjgxaLvYO6g1KLO6CAggRoUun5aEAQzgLVKZyUpABcapGlE+uHKaPP6EAItEKYWQFi7s2YQLDgAgIsgJFo6DWgLJBxXlYL6oVBsXkgN+H7zzG+X+7tPZFqDWrO/Nk+ni3xnSrHpOjH6efmib3rniEPQgpQmI7nvlqcOT7Z0wiDFCzdmoN9f3dKdQIUhIoMGmZBluQFgsNYgN0ejfeeH0XOLRsu6El63iZMqBzJeghPaJCj+QCoHRqG/b3FN2+9+MaLt9/64Mbv3n73T/cPHx5FvXHbAPdgMbKAZDgjCiJ6VwkyUAiJkVVjKQCXMxZb/WSCmHJKJLOCqfOudmollWVgp3yn42jHiW5oPHvis+qan9Ot4uRPeVE7iyt92ulP5LRfe7ybvP2h6viBeRKKLUqbOmmhTFNqEB4DAWeM462yvP3t11958c5v3/+0/On9tx98WPb2o5QWVlkll+ScGgpRVkJiA8XcLT4n9phcFM1AL8RUcLpz1DcpGSEfyN1h6IGhdACS5Mk9eTG1jLUIU+9obVaMhPtAlGhx+PDmUP78O6/8xz/7zo/v7fHo4UAUUN4sHIggGi0wAGZC1WhoSAVID+DSt+HsqwuaO88KEiWFqy1rWdYKjK70KpPkyT58EXO8ymanI8IjopLVyigfW6tav3Fnsf+T7wy/ePOX736q5U0udsbWiwwMQExl5VHkFgyUfFRTAC4V/+n+rwWc0NS/vm9kueiIcW+nLhlSRA6tTZInR9+8neYD94Z8IALFWEqRu4/NyN1ai8V6/eDlxfAf/uINw6//6d37a7nKACt9mLAYwb51kdlA55MhoEvcj9b1gFO3EsrUbu0uBpKULO1/kjzRZ07a2CbJJKOVCPNxBGKoLAz5uq0PMbabxhdj9R//4vt/9vrtsrpf4Jzn0RMQQ0Cg9MkEeW3TA/gCHmk/GFmARoqM/WHYmcZ45F2VJE9uTdr7aEwJPOjtnsZQJUutERHhpVgB3LGoC1+vB+B2wf/yo2858LN37qsCZRmMuSk3nLnSTQ/g8ax+jyFiKjRXARg9FhReEbf3d0qhj9771iZJ8kSsP2WUmaz/pk8NAovTWi/PIV3hChibWwRLtRJHd4v/5Rvf+unLt5d+AD8slKFYEF5Yasts7fQAHp/QPK17GkpNr9TSdGdvWcNFSsF0LZPkycG+AiMA2x4IGr2AGRCjp3hWqyET3ORLX7+8s/Nvf/StIH7zyaNH62DZjWCpi3W0oZaILARLD+Ay99+cvy5CVNHcjb5/ra33q93e2zNFMUPkFkCSPMGnz3BBB2sRwSlRNECnOdAkj2pWTVH96JUd/a8/eP17N+pNtCqn1bG1AfBxzM26FIDH0QD1ENDsAfQmtiTcb+/u7FQYxSwuT5InyrTen5xqB2USKUKm3loDQBELoLAg4c3DCVqMo44evLzEf/iLH3739u6yHVatrQQ8avZrSQF4HCeUgPW6RBNK9BxQGlEQ927dLBIZclneWEnyhAgiGG4hBBiETE64hReFSYbePYnTVGe2UlFAdwVKqYvlUHa1vmft3//59793Z7nwgwVahcxKPqgpAJe0/rBgb5olqmiKAol0aVGHF27dKgQ8yGn2X5IkT8oDCCL6YIjeNgphjKIomjaFS98aFuANaLYgawlRKnKLcb1U+8YO/t2ff/uH924u26pBQVPu1aUAXFIAelcSHG/vUr1ZuY97td7Z2xkGC4SVErkHkCRPUAA2D9x29sU86Ie9LkAwoQYqGW10jWZ9XgEV5gGicX3/5R3+u59+9417txeINq5tepHjx5ycBkPPlf/cfgspANf4FuzBnxDEZsVpFix0toPXbixfXlSDj0WS8gomyZNdfvV8u80vgRJj/kJ/NkGJII0AIiQ3i2JuVK31aN0IqxF3Cv7dj17/V/eWd8dPrR0Vo0D3QICSFF5iLGhmzkIYIdBBvyb1/Wm+zheAYJ+bIQiNbGagLNpu8Vdv7dw2Sa2ZJBQxswuS5CkJQd97C5rIIGNKBNI0QzJEVgMZbmpAExpIKxUqcpT10StL/Lvv3PtXr97ZaYcaV0DUOtQyQEGECCeCRSiSsQ81YlwTNyDrAD7DD9VJt1Dydmd/5+7NG9abo2PT/zglIEm+ktDQqS9uD1hQL81HSIqAGYvooba+s7/4Z298+xGX//DOR4cQFmWMVktRyAIDJWkanglys8N8DZ7q9AA+444zqg8476nHwdbu7u2/cOsWoF6eYnPfkiRJvg5V4Km5E5qGDTMkSqVWeYyr8dbOzr/54Q9+/I17y/C2Ply3tYqRrIGFq4abQtPom3J9ioZTAC64saYbYKpHB8IZA/Xy7v7NxSIiEDLl5UuSq6IEmzwfknWoJNrYTF4qi3Hh472Kf/nt1165sTvIy2DNm5U6BAfHEMG+LUAQNBmvhwakBbtw+b+5OH0enjDuD+Wl/b0FpwkBEBBhU2VikiRXgtaae5gVku4Oj0LVWJfD+6/d3PkX3//2zYH0VYT3JI4iGMIQYoiTY58eQDoBUy8I6zNOo93cGV5c7ljznidaQEg5DyZJvr6Fv82/TpjsiCBiqGZU+Bi+pq+Wth7awx+8fOf79+4uPQrh7q5NqD+m5Z5SAPLGwqkeDxLGG8t6s5r5aGa9QiCNf5JcNYZh6Gt/kmZWSqm1FgM5DrHaaUd//s3XvrG3W+UiwtAMQc3TKImpGVGGgK4xJA10BdnDPVGpuzf2btRiCkWQJFBrQcmrlSRf/yO7GfmyMf3u88BWb85wNGq919bfurX/3RduL71R3grHymZwwzX05lMALvYBEID1vGNCS8aeYVlgCEAms6BDriwFTpKv8Tnd/oVTK3eiT5WfFnE0lVgt1offvXvn9lAqQtRIuTEAkr379JTknQJwjZcT6n7A9B/4TrEby6GyZ4ROk4YC8imFOEmSr/w57dt08685JZTbD3J/loN0MxpNbWiH33nx9jdu7A3RKAWnMmPquOdEegB5Y0lASAFRsVfK/s4SDMkVMoEwkWF5DZPkaxaAc/5mchA20z0YUQTAZBhvDHr9hRu7gIXmCQScpSN6H8gUgGt7V8EkQiIdkgXD92vZW1ap9ZCPTQPCiFLSA0iSr8tT3/zafHHL6GNqHwRAhNODMoCjrx+8cvvG7VpKixoEyDAKVE8GvS4XMAXgfAHY/C4MMgLaqWVnqBEOyGzqSB45aDRJvm4n4FKWTlZUJXOLMG/t6M7+3t3d/WWoSBYkOHeD0PXp7ZICcA7bH77UHYJYkAuzYMhAkgZFRE4ES5Kv+Wm91BKMQGER2ECYVXK/lju7i4GiT1mf5LVrB53N4M4XAAcNBmgAMWoI7i+HUqiRZJUQGmFRSAWQPaGT5OsgaGe8AZ17mklkK0QLCiSsan13vxojGOgZH0GK12psTFqui24sBEGhwIpbFYsZ2K+YSRD7yDr2CFGSJFf6iYYQjRDECJNQFLsLktPo72nxx+tlElMALglJMkfKJcmz69kr1Mf8kQCMHOp1HxScIaBL2n4yR/8myTMtAEDf6ZWmeTJWCq73c50ewGMpQV6DJHlWl3HWo7gESakPgbnuj3QKwIW3y+w2Klu+Jcnz9FBvigSE6/5opwB8ps+Ypj9JnrMnetoF4KVLCFIArvlNg972P+UgSZ5t6y9JEGcFSA8gBeBzfMZpqZDh/yR5Pp5oUEJfzhmvuwFMAUiSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkeY7IVhBfij6IOkmSK845bUKnQZBxqvm/rlN5cJqvCy07NSUO9xxQEQFtpkUzawKS5Bl6ogHABOsjnE4ODg5MNWIG9JYv18UwpgB8KZFIkuQZlAJSF8x+uWYT/lIAkiRJrikpAEmSJCkASZIkSQpAkiRJkgKQJEmSpABcdzLpJ0nyUU4BuA53yNz+n+zZnjkOIEmeh+e6H8nzp8FIfUrAdXnWUwCSJEnSA0iSJElSAJIkSZIUgCRJkiQFIEmSJEkBeP5hTP8VRQXR/8zeFRQSr1vbqCR5Lp5skWB/ok91c4/pieb16fOYAnCB9Te4WkGRGIgwOI2gCYSTDZBfp7axSfJMI3R7T/R0bjM3hlGAyXqnaMFEmDa9QlMArrMKGCijNo3DKRRMGqAgAcvKgCR5ZjSAOP3Q8rrbwBSAz3AV0UfBHC8hzpSCnRgrkSTJFSaOa7+O/2vT9KcgYg786vrM+kgBuKT/KEkgOE+WS9OfJM/ecwwRMgUVJhhgiq3tPAWl6zTpKQXgcrfNZPB5vPBXTgRLkmfMB9C0guPmEaZgkE0D/jQ3g0gBuN7wpMknwCnvx6ZUIDD9gCR5lhZyxCZ578RjG6f6AqUHkAIAUifukm7zRYjH00TTD0iSZ8T6d8t//LjOHsDJVdw18+5TAC6+Y6SQrJgkd5EkQdJIbSUKn9goTpLkykoAKSlCpRaSIgSBxv5sT8u669X2NwXgQusPICSAZOn+QAuE5AII29wx2SU6Sa6+Ty+GQLIUcw8PmFloiuxqLu2keK2WdCkA5xOQ1QrAWwNppQQQIY/pXglSxiA8p0skyTOhAaEiMytrb03REDI6FaDD5s1gXKvyzpq3xcX3C9D3h4joJQBG2lQHBsW0D5whoCR5Jh5oUkBrUWoVq8ucdLBt5YEaiymUewB5rwQkwswkNPfRW0BWC2kBiz40iEQKQJJcfYTSN3clQ5GxLIYHDx96hGBBC9p0HkDZNXmqUwDOvVXQ94tIynqaAA8ODg4eHRx3iUuS5Jla0/XZrqXW0VsIDr7zwYcrD9mUHhqErlleXwrAxTIgydgLgEup9x88+OjTT1yKPivYbDpHqQhJcuXtP0gYZGShGYq9/d57H37ycRBh5ZQ95LXJ7Mg9gHPvFUiCDIwQSLEM9w/11ieHr77E/bqsGgdEDVcbDYTV2X9MroQDl3wxEymop8L0m78fqTL/fh6W3ltqziecOR6fhqmXop7EQ/nlPm0CsBYKse3sP3D8/J2PPh5NZWEiAUEU2Fd41yaumwJw7k1lhDBFgUCxiVze+Kf3HyxufPLP3/jm0h+VdlQgtaPlcrFWVVYEXxnDvrE/lzYMfAwb8jge5Bx74CVOPjdo8Vjf7qLIx+ecvDmBZEhQN4bWHwQA1qvfJ3M+EdP/DTpxJAzY+gAEnfjzF47fXPCpXrpvs8QRXJeCvZsfjP43v/n9P73/4KjelC1qzLpFiYxeCHY9OkKnAFxkRLi5uygE2Dg8dP38rfeHxfCT1+8Ng1nDzp6tvTmfwC2efJ751yVF4SKDe27FhqDHEIDHMsnHtUUnDdl5b4PnbTpedFOdu9q4qB6Fn/dVndAFTi1Oeht0qifQT4/B9j9kb5gO6Mzx5Pf8DEPKrX3XLyZvlxdJVxlRfWfvvYeHf/eHP/38zfc+XaHsLECzmC62ILGnAF0XFyAF4LPWeprvYMlIRCkfPTz8n7/8/bhaf+ve7Rf39m4OJXBYDfPEsOSrVYXHidXyXLtJXND8S4+xyj7fNJXLvz3pMZyeOP/rPPdLlxfOizXyone8cReOjxG6cEE1BZc2x9mn0NSGbdLis8fTlv74r/VZrdtOvMoIfHo0vvnH3//unfffPTg8Ksthfzl2X9960F/XMHyYAvCZoQFJnG7foLWodbF3v63/xy//8Pt3916/d/fmzhDjyJIbAFdIAKTzrZ7O2iZeaODOfeW4aMP/fLthZ79+4aL+XLvJ879VnC9O53/xYr/nnPd/eUdW0V/ZgNg6nm/94zyLPBv/abtAU2FNnHxNbglk99YkGRn96K4zWnGejNDCdf/RwcP1+jAQy91WF40WLH3ok1GUjhuECtekzWMKwOfc51Tv/sY+QdQlt0WUxduP2tufvjUYyjCsgsp8qqvjAfCiJfm5YZYLV8OXD6icb2SDl9cKPU7E6XTzys94axdoIS/yPy5v9GIzF+OE/b3gIrN3Wdj+37TlrI0My7TZd4bJYw5BnWrgSYTm47GA4NRvNG1UT0cviGpVdYhS3cqaCIHGIoKSjvt7Ubw+W3opAJeTgX6D1wKZR8CKx1qlDrWEHGa5B/z0bf0Xtpy8ILjw+EEk8rNf7eRN83hFgmdfxXh+zCrOs9Rx0R7ARbvAPG8f4NKaRfJxPhE7Hf4RjncL5gCSph3j+fdbJ8ekBBBhQszHIm6Sj3oUtv/mbHaSI1xRah1DoxxWSmV4aIoC4nqmj6UAXOLJlOYVi0tU/89iCXHdF2QaMaURnPNUn5cl90VO04U5d0/stLNL56t02paNnLP2TBaUiZsjdWI1SDEYJguGwTbrz+NvO61jz09p5Nl74Zz/4cSrTUtbsPDU2njbXzgbrYiTX+wRJ50NiwjnlqlepDUXR3W2GiPbZ3U2PHeDOhQXKcNF32tqx7x1nH7o2fzG/KXp9/PxOB7Ttw22jjGrwtkIUJw+Mlg9LCgZQUWEwgmjZFtKFUCmgV53Ygri9tXW5MwrQBCFUETfPIJMAAvgoSLGscM6LV5M7H+1ffyCp5054bFPw9ZW3NnTMAvdVTytryLnVXB/ogNF9O3jmdMoIlCm1zxeO/LYYkzRiQjZqeOmi/z8MXHrFTZHnRSm/t54asF7HMwgOb8DXXCMzcq4xyfI2Do+xjbEZ7gb53tXvLTvxTnB/5wd2y/4Pp7yOm66tFIBpCBUINuy/lPyKzd7wikA1x5DxKlA0NQjQoTj+ObhxTc0LzhewdP4mQ/q13vaE/kZvyJjc2Yz89wN0i9yGvUkX+2C087u7n6Z065EdKUvC+btBs35X7DjxR62bsi4JkGhFIDP9wRMx7eCKM3jQ+f0hf6fsJPe6nwrMRAGbB+/2GlzePNLnbZ15jmnbQetn4/Tzr2wT/a0p/qhnz3tYtu0MVvbxy92ms6c8GVOuxqWdN5gmIa7nrfxHddvJy8F4LGWdZv8AMUUZ4jc/k2SZ+QBjh612mRRaWP3ta1V16gmIAXgosjP8X8Dc3uBORzMyS3Y3D0mIs4PB1Pof7V9/GKn6cwJj30ajuO155yG7cj4c3LauRf28U57Op/mFzxtvi11QaTLTh6/2Gk6c8KXOe2qxFIMMV3RrTr/6YOeHu3Y/ADXJCUoBeASMSCcyF3YJJgADhTLdqDJVxnJePY8Tl6NoirvHS14LEoMGKjgtjDGHITLQrBrb/k3C675qdsEn3tEsfT0ZpsyzdQ3BuzE6og6TmvWnHT8RU7jlN/4pU7bLHDOPW17/fZ8nHbuhX3c057Gp/llTpO07ZJuEo9ATf3sTxy/2Gm68DSc6MXw+addDWMqTuHbY09+Kv3qx5j8gU1SKu069IPjv/7P/ymN/XmLFmHuunWceCduueWa4wjXt4okSSfgGXrHm3exafW4lf7aH2dtPACR10EA0gO44F7ZWrNsJQzoVM3g1h2Se8HJV3d3Jl9kVXdipxfcroaOjTTMDU2vx0VOAUiSJKX02XWvvhTZwixJkuSakgKQJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZKkACRJkiQpAEmSJEkKQJIkSZICkCRJkqQAJEmSJCkASZIkSQpAkiRJkgKQJEmSpAAkSZIkKQBJkiRJCkCSJEmSApAkSZICkJcgSZIkBSBJkiRJAUiSJElSAJIkSZIUgCRJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJEmSFIAkSZIkBSBJkiRJAUiSJElSAJIkSZIUgCRJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJElSAJIkSZIUgCRJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJEmSFIAkSZIkBSBJkiRJAUiSJElSAJIkSZIUgCRJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJEmSFIAkSZIkBSBJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJEmSFIAkSZIkBSBJkiRJAUiSJElSAJIkSZIUgCRJkiQFIEmSJEkBSJIkSVIAkiRJkhSAJEmSJAUgSZIkSQFIkiRJUgCSJEmSFIAkSZLkMvz/AVad4QrwbWiaAAAAAElFTkSuQmCC"


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify(
        {
            "name": "Agenda Cortinas",
            "short_name": "Cortinas",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#355347",
            "theme_color": "#355347",
            "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
        }
    )


@app.get("/icon.png")
def icon_png():
    import base64
    return app.response_class(base64.b64decode(ICON_PNG), mimetype="image/png")


@app.get("/logo.png")
def logo_png():
    import base64
    return app.response_class(base64.b64decode(LOGO_PNG), mimetype="image/png")


@app.get("/icon.svg")
def icon_svg():
    return icon_png()


@app.get("/sw.js")
def service_worker():
    js = """self.addEventListener('push', event => {
  let data = {titulo:'Agenda Cortinas', texto:'Tienes un aviso', badge:1};
  try { data = Object.assign(data, event.data.json()); } catch(e) {}
  const n = Number(data.badge||1);
  event.waitUntil((async () => {
    if (self.registration.setAppBadge) await self.registration.setAppBadge(n);
    await self.registration.showNotification(data.titulo||'Agenda Cortinas', {
      body: data.texto||'',
      tag: 'agenda',
      renotify: true,
      data
    });
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});
"""
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/api/push-key")
def api_push_key():
    return jsonify({"key": VAPID_PUBLIC})


@app.post("/api/push-sub")
@login_required
def api_push_sub():
    body = request.get_json(silent=True) or {}
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "Suscripción incompleta"}), 400
    db = load_db()
    user = current_user()
    db["push"] = [s for s in db.get("push") or [] if s.get("endpoint") != endpoint]
    db["push"].append({"usuario": user["usuario"], "endpoint": endpoint, "keys": keys})
    save_db(db)
    return jsonify({"ok": True})


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
    return jsonify({"user": current_user(), "instaladores": [public_user(u) for u in instaladores(db)], "clientes": db.get("clientes") or []})


@app.get("/api/copia")
@login_required
def api_copia():
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño puede bajar la copia"}), 403
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if DATA_FILE.exists():
            zf.write(DATA_FILE, arcname="data.json")
        if UPLOAD_DIR.exists():
            for f in UPLOAD_DIR.iterdir():
                if f.is_file():
                    zf.write(f, arcname="uploads/" + f.name)
        if BACKUP_DIR.exists():
            for f in BACKUP_DIR.iterdir():
                if f.is_file():
                    zf.write(f, arcname="backups/" + f.name)
    buf.seek(0)
    nombre = "agenda-copia-" + datetime.now().strftime("%Y%m%d-%H%M") + ".zip"
    return send_file(buf, as_attachment=True, download_name=nombre, mimetype="application/zip")


@app.get("/api/clientes")
@login_required
def api_clientes():
    db = load_db()
    return jsonify({"clientes": db.get("clientes") or []})


@app.post("/api/clientes")
@login_required
def api_crear_cliente():
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño"}), 403
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "").strip()
    email = (body.get("email") or "").strip()
    if not nombre:
        return jsonify({"error": "Pon el nombre del cliente"}), 400
    db = load_db()
    c = guardar_cliente(db, nombre, email)
    save_db(db)
    return jsonify({"cliente": c, "clientes": db.get("clientes") or []})


@app.patch("/api/clientes/<cid>")
@login_required
def api_editar_cliente(cid: str):
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño"}), 403
    body = request.get_json(silent=True) or {}
    db = load_db()
    c = next((x for x in db.get("clientes") or [] if x.get("id") == cid), None)
    if not c:
        return jsonify({"error": "Tienda no encontrada"}), 404
    if "nombre" in body:
        nombre = (body.get("nombre") or "").strip()
        if not nombre:
            return jsonify({"error": "El nombre no puede estar vacío"}), 400
        c["nombre"] = nombre
    if "email" in body:
        c["email"] = (body.get("email") or "").strip()
    db["clientes"].sort(key=lambda x: (x.get("nombre") or "").lower())
    save_db(db)
    return jsonify({"cliente": c, "clientes": db.get("clientes") or []})


@app.delete("/api/clientes/<cid>")
@login_required
def api_borrar_cliente(cid: str):
    if current_user()["rol"] != "dueno":
        return jsonify({"error": "Solo el dueño"}), 403
    db = load_db()
    db["clientes"] = [c for c in db.get("clientes") or [] if c.get("id") != cid]
    save_db(db)
    return jsonify({"ok": True, "clientes": db.get("clientes") or []})


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
    email = (body.get("email") or "").strip()
    nuevo = {"usuario": usuario, "password": password, "nombre": nombre, "rol": "instalador", "email": email}
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
    if "email" in body:
        u["email"] = (body.get("email") or "").strip()
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
    try:
        procesar_recordatorios(db)
    except Exception:
        pass
    user = current_user()
    changed = False
    for t in db["trabajos"]:
        if t.get("estado") == "nueva" and (t.get("asignado_a") or "").strip():
            t["estado"] = "asignada"
            changed = True
    if changed:
        save_db(db)
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
            "clientes": db.get("clientes") or [],
        }
    )


@app.post("/api/trabajos")
@login_required
def api_crear():
    user = current_user()
    if user["rol"] not in ("dueno", "instalador"):
        return jsonify({"error": "No puedes crear trabajos"}), 403
    if request.files or request.form:
        body = request.form.to_dict()
        body["tareas"] = request.form.getlist("tareas")
        files = request.files.getlist("archivos") or request.files.getlist("archivo")
    else:
        body = request.get_json(silent=True) or {}
        files = []
    if (body.get("fase") or "") == "reparto" and not (body.get("cliente") or "").strip():
        body["cliente"] = "Reparto"
    if not (body.get("cliente") or "").strip():
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400
    db = load_db()
    if user["rol"] == "instalador":
        body["asignado_a"] = user["usuario"]
        body["asignado_nombre"] = user["nombre"]
        inst = find_user(db, user["usuario"])
    else:
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
        asignar(trabajo, inst, user, db, "reparto" if trabajo["fase"]=="reparto" else ("toma de medidas" if trabajo["fase"] == "medidas" else "instalación"))
    elif trabajo.get("fase") in ("medidas", "instalacion", "reparto"):
        return jsonify({"error": "Elige el instalador antes de guardar"}), 400
    if trabajo.get("fase") != "reparto":
        guardar_cliente(db, trabajo.get("cliente"), trabajo.get("email"))
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

    fase_txt = "reparto" if trabajo.get("fase")=="reparto" else ("toma de medidas" if trabajo.get("fase") == "medidas" else "instalación")
    instalacion_creada = None

    if user["rol"] == "dueno" and body.get("asignado_a"):
        inst = find_user(db, body.get("asignado_a"))
        if not inst or inst["rol"] != "instalador":
            return jsonify({"error": "Instalador no válido"}), 400
        asignar(trabajo, inst, user, db, fase_txt)

    if "estado" in body:
        nuevo = body["estado"]
        if nuevo == "deshacer_incidencia":
            previo = trabajo.get("estado_prev") or ""
            if previo not in STATUSES or previo == "incidencia":
                if trabajo.get("cita_fecha"):
                    previo = "cita"
                else:
                    previo = "asignada" if trabajo.get("asignado_a") else "nueva"
            trabajo["estado"] = previo
            trabajo["estado_prev"] = ""
            add_msg(trabajo, user, "Incidencia cancelada. Sigue como estaba.")
            trabajo["actualizado"] = now_iso()
            save_db(db)
            return jsonify({"trabajo": trabajo, "instalacion_creada": None})
        if nuevo not in STATUSES:
            return jsonify({"error": "Estado no válido"}), 400
        if nuevo == "facturado" and user["rol"] != "dueno":
            return jsonify({"error": "Solo el dueño puede marcar facturado"}), 403
        if nuevo == "finalizada" and trabajo.get("fase") == "instalacion":
            if trabajo.get("estado") == "incidencia":
                if not str(trabajo.get("rieles_incidencia") or body.get("rieles_incidencia") or "").strip():
                    return jsonify({"error": "Pon los trabajos realizados en la incidencia"}), 400
            elif not str(trabajo.get("rieles") or body.get("rieles") or "").strip():
                return jsonify({"error": "Pon cuántos rieles se han instalado antes de terminar"}), 400
        anterior = trabajo["estado"]
        if nuevo == "incidencia" and anterior != "incidencia":
            trabajo["estado_prev"] = anterior
        trabajo["estado"] = nuevo
        if nuevo == "en_curso" and anterior != "en_curso":
            add_msg(trabajo, user, f"Empezado ({fase_txt}).")
            add_alerta(
                db,
                para="dueno",
                texto=f"{user['nombre']} ha empezado la {fase_txt} de {ficha_aviso(trabajo)}.",
                trabajo_id=trabajo["id"],
                tipo="empezada",
            )
        if nuevo == "incidencia" and anterior != "incidencia":
            if trabajo.get("fase") != "instalacion":
                return jsonify({"error": "La incidencia solo es para instalaciones"}), 400
            add_msg(trabajo, user, "Incidencia abierta. El trabajo sigue pendiente.")
            add_alerta(
                db,
                para="dueno",
                texto=f"Incidencia en la instalación de {ficha_aviso(trabajo)}.",
                trabajo_id=trabajo["id"],
                tipo="incidencia",
            )
            if trabajo.get("asignado_a") and user["usuario"] != trabajo.get("asignado_a"):
                add_alerta(
                    db,
                    para=trabajo["asignado_a"],
                    texto=f"Hay una incidencia en {ficha_aviso(trabajo)}.",
                    trabajo_id=trabajo["id"],
                    tipo="incidencia",
                )
            nota_ya = (trabajo.get("incidencia_nota") or body.get("incidencia_nota") or "").strip()
            if trabajo.get("email") and nota_ya:
                trabajo["incidencia_nota"] = nota_ya
                aviso = enviar_correo_cliente(trabajo, fase_txt, motivo="incidencia")
                add_msg(trabajo, user, "Correo de incidencia: " + aviso)
        if nuevo == "finalizada" and anterior != "finalizada":
            if anterior == "incidencia":
                add_msg(trabajo, user, "Incidencia acabada. Instalación terminada.")
                add_alerta(
                    db,
                    para="dueno",
                    texto=f"{user['nombre']} ha cerrado la incidencia de {ficha_aviso(trabajo)}.",
                    trabajo_id=trabajo["id"],
                    tipo="incidencia_cerrada",
                )
            else:
                add_msg(trabajo, user, f"Finalizado ({fase_txt}).")
                add_alerta(
                    db,
                    para="dueno",
                    texto=f"{user['nombre']} ha terminado la {fase_txt} de {ficha_aviso(trabajo)}.",
                    trabajo_id=trabajo["id"],
                    tipo="finalizada",
                )
            if trabajo.get("email") and body.get("enviar_correo_fin"):
                motivo_fin = "incidencia_fin" if anterior == "incidencia" else "terminada"
                aviso = enviar_correo_cliente(trabajo, fase_txt, motivo=motivo_fin)
                add_msg(trabajo, user, "Correo al cliente: " + aviso)
            elif trabajo.get("email"):
                add_msg(trabajo, user, "Terminado sin enviar correo.")
            if trabajo.get("fase") == "medidas" and not trabajo.get("relacionado_id") and anterior != "incidencia":
                inst_job = crear_instalacion_desde(trabajo, user)
                trabajo["relacionado_id"] = inst_job["id"]
                inst_job["relacionado_id"] = trabajo["id"]
                db["trabajos"].append(inst_job)
                instalacion_creada = inst_job
                add_msg(trabajo, user, "Se ha creado la instalación de este cliente.")
                add_alerta(
                    db,
                    para="dueno",
                    texto=f"Medidas de {ficha_aviso(trabajo)} listas. Falta enviar la instalación.",
                    trabajo_id=inst_job["id"],
                    tipo="instalacion_lista",
                )

    if body.get("enviar_correo"):
        fase_mail = "toma de medidas" if trabajo.get("fase") == "medidas" else "instalación"
        aviso = enviar_correo_cliente(trabajo, fase_mail)
        add_msg(trabajo, user, "Correo al cliente: " + aviso)
        add_alerta(
            db,
            para="dueno",
            texto=f"Correo de {fase_mail} terminada: {aviso}",
            trabajo_id=trabajo["id"],
            tipo="correo",
        )

    if "incidencia_nota" in body:
        trabajo["incidencia_nota"] = (body.get("incidencia_nota") or "").strip()
        if trabajo["incidencia_nota"]:
            add_msg(trabajo, user, "Nota incidencia: " + trabajo["incidencia_nota"])
            if trabajo.get("estado") == "incidencia" and trabajo.get("email") and body.get("enviar_correo_incidencia"):
                fase_inc = "instalación" if trabajo.get("fase") == "instalacion" else "toma de medidas"
                aviso = enviar_correo_cliente(trabajo, fase_inc, motivo="incidencia")
                add_msg(trabajo, user, "Correo de incidencia: " + aviso)

    if user["rol"] == "dueno" and body.get("fase") in FASES:
        anterior_fase = trabajo.get("fase")
        trabajo["fase"] = body["fase"]
        if anterior_fase != trabajo["fase"]:
            add_msg(trabajo, user, "Cambiado a " + ("reparto" if trabajo["fase"]=="reparto" else ("toma de medidas" if trabajo["fase"]=="medidas" else "instalación")) + ".")
    for field in ("cita_fecha", "cita_hora", "cita_nota", "cliente", "cliente_final", "email", "email2", "email_final", "email_final2", "telefono", "telefono2", "telefono_final", "telefono_final2", "direccion", "localidad", "cp", "tipo", "medidas", "rieles", "rieles_incidencia"):
        if field in body and user["rol"] == "dueno":
            trabajo[field] = (body.get(field) or "").strip()
        elif field in body and field in ("cita_fecha", "cita_hora", "cita_nota", "rieles", "rieles_incidencia"):
            trabajo[field] = (body.get(field) or "").strip()
    if "tareas" in body:
        trabajo["tareas"] = [str(x).strip() for x in (body.get("tareas") or []) if str(x).strip()]
    if user["rol"] == "dueno" and ("cliente" in body or "email" in body):
        guardar_cliente(db, trabajo.get("cliente"), trabajo.get("email"))

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
        if body.get("enviar_correo_cita") and trabajo.get("email"):
            motivo_cita = "incidencia_cita" if trabajo.get("estado") == "incidencia" else "cita"
            aviso = enviar_correo_cliente(trabajo, fase_txt, motivo=motivo_cita)
            add_msg(trabajo, user, "Correo de cita: " + aviso)
        add_alerta(
            db,
            para="dueno",
            texto=f"{user['nombre']} ha quedado con {ficha_aviso(trabajo)} el {cuando} ({fase_txt}).",
            trabajo_id=trabajo["id"],
            tipo="cita",
        )
        if trabajo.get("asignado_a") and user["rol"] == "dueno":
            add_alerta(
                db,
                para=trabajo["asignado_a"],
                texto=f"Cita con {ficha_aviso(trabajo)} el {cuando} ({fase_txt}).",
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
            texto=f"{user['nombre']} ha subido un archivo de {ficha_aviso(trabajo)}.",
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
    foto = next((f for f in (trabajo.get("fotos") or []) if f.get("id") == foto_id), None)
    if not foto:
        foto = next((f for f in (trabajo.get("archivos") or []) if f.get("id") == foto_id), None)
    if not foto:
        return jsonify({"error": "Archivo no encontrado"}), 404
    trabajo["fotos"] = [f for f in trabajo.get("fotos") or [] if f.get("id") != foto_id]
    trabajo["archivos"] = [f for f in trabajo.get("archivos") or [] if f.get("id") != foto_id]
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
    mias = {a["id"] for a in alertas_de(db, user)}
    if alerta_id:
        if alerta_id in mias:
            db["alertas"] = [a for a in db["alertas"] if a["id"] != alerta_id]
    else:
        db["alertas"] = [a for a in db["alertas"] if a["id"] not in mias]
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
