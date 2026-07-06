from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
from dotenv import load_dotenv
import csv, io, os, requests, json, secrets

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ══════════════════════════════════════
#  BANCO
# ══════════════════════════════════════
_db_url = os.environ.get("DATABASE_URL", "sqlite:///tasks.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ══════════════════════════════════════
#  FLASK-MAIL (Gmail SMTP)
# ══════════════════════════════════════
app.config["MAIL_SERVER"]   = os.environ.get("MAIL_SERVER",   "smtp.gmail.com")
app.config["MAIL_PORT"]     = int(os.environ.get("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"]  = True
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_USERNAME", "noreply@todopro.com")
mail = Mail(app)

APP_URL = os.environ.get("APP_URL", "http://localhost:5000")

# E-mail do admin — configure via variável de ambiente
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@todopro.com")


# ══════════════════════════════════════
#  MODELOS
# ══════════════════════════════════════
class Usuario(db.Model):
    __tablename__ = "usuarios"
    id                 = db.Column(db.Integer, primary_key=True)
    nome               = db.Column(db.String(80),  nullable=False)
    email              = db.Column(db.String(120), nullable=False, unique=True)
    senha_hash         = db.Column(db.String(256), nullable=False)
    is_admin           = db.Column(db.Boolean, default=False)
    ativo              = db.Column(db.Boolean, default=True)
    criado_em          = db.Column(db.DateTime, default=datetime.utcnow)
    email_verificado   = db.Column(db.Boolean, default=False)
    token_verificacao  = db.Column(db.String(64), nullable=True)
    token_expira       = db.Column(db.DateTime,   nullable=True)
    token_reset_senha  = db.Column(db.String(64), nullable=True)
    token_reset_expira = db.Column(db.DateTime,   nullable=True)
    tarefas            = db.relationship("Tarefa", backref="usuario", lazy=True, cascade="all, delete-orphan")

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def check_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    def to_dict(self, admin=False):
        d = {"id": self.id, "nome": self.nome, "email": self.email, "is_admin": self.is_admin}
        if admin:
            d["ativo"]     = self.ativo
            d["criado_em"] = self.criado_em.isoformat() if self.criado_em else None
            d["total_tarefas"]     = len(self.tarefas)
            d["tarefas_concluidas"] = sum(1 for t in self.tarefas if t.concluida)
        return d


class Tarefa(db.Model):
    __tablename__ = "tarefas"
    id         = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    descricao  = db.Column(db.Text, nullable=False)
    prazo      = db.Column(db.String(10), nullable=True)
    hora       = db.Column(db.String(5),  nullable=True)
    concluida  = db.Column(db.Integer, default=0)
    categoria  = db.Column(db.String(50), nullable=True)
    prioridade = db.Column(db.String(10), default="media")
    ordem      = db.Column(db.Integer, default=0)
    criada_em  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "descricao": self.descricao,
            "prazo": self.prazo, "hora": self.hora,
            "concluida": self.concluida, "categoria": self.categoria,
            "prioridade": self.prioridade, "ordem": self.ordem,
            "criada_em": self.criada_em.isoformat() if self.criada_em else None,
        }


def _enviar_email(destinatario, assunto, corpo_html):
    """Tenta enviar e-mail; retorna True se enviou, False se SMTP não configurado."""
    if not app.config["MAIL_USERNAME"]:
        return False
    try:
        msg = Message(assunto, recipients=[destinatario], html=corpo_html)
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.error(f"[MAIL] Falha ao enviar para {destinatario}: {e}")
        return False


def _html_verificacao(nome, link):
    return f"""
    <div style="font-family:sans-serif;max-width:520px;margin:auto;padding:32px;background:#0f1724;color:#e2e8f0;border-radius:16px;">
      <h2 style="color:#38bdf8;margin-bottom:8px;">Oi, {nome}!</h2>
      <p style="color:#94a3b8;margin-bottom:24px;">Clique no botão abaixo para verificar seu e-mail e ativar sua conta no <strong style="color:#e2e8f0">To-Do Pro</strong>.</p>
      <a href="{link}" style="display:inline-block;padding:13px 28px;background:linear-gradient(135deg,#38bdf8,#818cf8);color:white;text-decoration:none;border-radius:10px;font-weight:700;font-size:15px;">Verificar e-mail</a>
      <p style="margin-top:24px;font-size:12px;color:#475569;">Link válido por 24 horas. Se não foi você, ignore este e-mail.</p>
    </div>"""


def _html_reset(nome, link):
    return f"""
    <div style="font-family:sans-serif;max-width:520px;margin:auto;padding:32px;background:#0f1724;color:#e2e8f0;border-radius:16px;">
      <h2 style="color:#f87171;margin-bottom:8px;">Redefinir senha</h2>
      <p style="color:#94a3b8;margin-bottom:24px;">Olá, <strong style="color:#e2e8f0">{nome}</strong>. Clique abaixo para criar uma nova senha no <strong style="color:#e2e8f0">To-Do Pro</strong>.</p>
      <a href="{link}" style="display:inline-block;padding:13px 28px;background:linear-gradient(135deg,#f87171,#818cf8);color:white;text-decoration:none;border-radius:10px;font-weight:700;font-size:15px;">Redefinir senha</a>
      <p style="margin-top:24px;font-size:12px;color:#475569;">Link válido por 1 hora. Se não solicitou, ignore este e-mail.</p>
    </div>"""


def criar_admin():
    """Cria conta admin se não existir."""
    with app.app_context():
        db.create_all()
        if not Usuario.query.filter_by(email=ADMIN_EMAIL).first():
            admin_senha = os.environ.get("ADMIN_PASSWORD", "admin123")
            u = Usuario(nome="Admin", email=ADMIN_EMAIL, is_admin=True, email_verificado=True)
            u.set_senha(admin_senha)
            db.session.add(u)
            db.session.commit()
            print(f"[ADMIN] Conta admin criada: {ADMIN_EMAIL} / {admin_senha}")

criar_admin()


# ══════════════════════════════════════
#  DECORATORS
# ══════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "usuario_id" not in session:
            if request.is_json:
                return jsonify({"erro": "Não autenticado"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        u = db.session.get(Usuario, session.get("usuario_id"))
        if not u or not u.is_admin:
            if request.is_json:
                return jsonify({"erro": "Acesso negado"}), 403
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def usuario_atual():
    return db.session.get(Usuario, session.get("usuario_id"))

def tarefa_ou_404(id):
    uid = session.get("usuario_id")
    t = Tarefa.query.filter_by(id=id, usuario_id=uid).first()
    if not t:
        return None, jsonify({"erro": "Tarefa não encontrada"}), 404
    return t, None, None


# ══════════════════════════════════════
#  PÁGINAS
# ══════════════════════════════════════
@app.route("/")
def root():
    if "usuario_id" in session:
        return redirect(url_for("index"))
    return redirect(url_for("login_page"))

@app.route("/app")
@login_required
def index():
    return render_template("index.html", usuario=usuario_atual())

@app.route("/login")
def login_page():
    if "usuario_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/admin")
@login_required
@admin_required
def admin_page():
    return render_template("admin.html", usuario=usuario_atual())

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ══════════════════════════════════════
#  AUTH API
# ══════════════════════════════════════
@app.route("/auth/register", methods=["POST"])
def register():
    data  = request.get_json()
    nome  = (data.get("nome") or "").strip()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    if not nome or not email or not senha:
        return jsonify({"erro": "Preencha todos os campos"}), 400
    if len(senha) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
    if Usuario.query.filter_by(email=email).first():
        return jsonify({"erro": "E-mail já cadastrado"}), 409

    token = secrets.token_urlsafe(32)
    u = Usuario(
        nome=nome, email=email,
        email_verificado=False,
        token_verificacao=token,
        token_expira=datetime.utcnow() + timedelta(hours=24),
    )
    u.set_senha(senha)
    db.session.add(u)
    db.session.commit()

    link = f"{APP_URL}/auth/verificar/{token}"
    enviou = _enviar_email(email, "Verifique seu e-mail — To-Do Pro", _html_verificacao(nome, link))

    return jsonify({
        "ok": True,
        "verificacao_enviada": enviou,
        "usuario": u.to_dict(),
    }), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data  = request.get_json()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    u = Usuario.query.filter_by(email=email).first()
    if not u or not u.check_senha(senha):
        return jsonify({"erro": "E-mail ou senha incorretos"}), 401
    if not u.ativo:
        return jsonify({"erro": "Conta desativada. Entre em contato com o admin."}), 403
    if not u.email_verificado:
        return jsonify({"erro": "E-mail não verificado. Verifique sua caixa de entrada.", "nao_verificado": True}), 403

    session["usuario_id"]   = u.id
    session["usuario_nome"] = u.nome
    session["is_admin"]     = u.is_admin
    return jsonify({"ok": True, "usuario": u.to_dict(), "is_admin": u.is_admin})


@app.route("/auth/me")
@login_required
def me():
    return jsonify(usuario_atual().to_dict())


@app.route("/auth/verificar/<token>")
def verificar_email(token):
    u = Usuario.query.filter_by(token_verificacao=token).first()
    if not u:
        return render_template("login.html", msg_erro="Link de verificação inválido.")
    if u.token_expira and datetime.utcnow() > u.token_expira:
        return render_template("login.html", msg_erro="Link expirado. Faça o cadastro novamente.")
    u.email_verificado  = True
    u.token_verificacao = None
    u.token_expira      = None
    db.session.commit()
    return render_template("login.html", msg_ok="E-mail verificado! Agora faça login.")


@app.route("/auth/reenviar-verificacao", methods=["POST"])
def reenviar_verificacao():
    data  = request.get_json()
    email = (data.get("email") or "").strip().lower()
    u = Usuario.query.filter_by(email=email).first()
    if not u or u.email_verificado:
        return jsonify({"ok": True})  # resposta neutra por segurança
    token = secrets.token_urlsafe(32)
    u.token_verificacao = token
    u.token_expira      = datetime.utcnow() + timedelta(hours=24)
    db.session.commit()
    link = f"{APP_URL}/auth/verificar/{token}"
    _enviar_email(email, "Verifique seu e-mail — To-Do Pro", _html_verificacao(u.nome, link))
    return jsonify({"ok": True})


@app.route("/auth/esqueci-senha", methods=["POST"])
def esqueci_senha():
    data  = request.get_json()
    email = (data.get("email") or "").strip().lower()
    u = Usuario.query.filter_by(email=email).first()
    if u and u.email_verificado:
        token = secrets.token_urlsafe(32)
        u.token_reset_senha  = token
        u.token_reset_expira = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()
        link = f"{APP_URL}/auth/reset-senha/{token}"
        _enviar_email(email, "Redefinir senha — To-Do Pro", _html_reset(u.nome, link))
    return jsonify({"ok": True})  # sempre neutro para não revelar quais e-mails existem


@app.route("/auth/reset-senha/<token>")
def reset_senha_page(token):
    u = Usuario.query.filter_by(token_reset_senha=token).first()
    if not u or (u.token_reset_expira and datetime.utcnow() > u.token_reset_expira):
        return render_template("login.html", msg_erro="Link de redefinição inválido ou expirado.")
    return render_template("login.html", reset_token=token)


@app.route("/auth/reset-senha", methods=["POST"])
def reset_senha():
    data  = request.get_json()
    token = data.get("token") or ""
    nova  = data.get("nova_senha") or ""
    if len(nova) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
    u = Usuario.query.filter_by(token_reset_senha=token).first()
    if not u or (u.token_reset_expira and datetime.utcnow() > u.token_reset_expira):
        return jsonify({"erro": "Link inválido ou expirado"}), 400
    u.set_senha(nova)
    u.token_reset_senha  = None
    u.token_reset_expira = None
    db.session.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════
#  ADMIN API
# ══════════════════════════════════════

# Stats gerais
@app.route("/admin/stats")
@login_required
@admin_required
def admin_stats():
    total_usuarios  = Usuario.query.filter_by(is_admin=False).count()
    usuarios_ativos = Usuario.query.filter_by(is_admin=False, ativo=True).count()
    total_tarefas   = Tarefa.query.count()
    tarefas_feitas  = Tarefa.query.filter_by(concluida=1).count()
    hoje = datetime.now().strftime("%Y-%m-%d")
    atrasadas = Tarefa.query.filter(Tarefa.prazo < hoje, Tarefa.concluida == 0).count()

    # Novos usuários nos últimos 7 dias
    from datetime import timedelta
    sete_dias = datetime.utcnow() - timedelta(days=7)
    novos = Usuario.query.filter(Usuario.criado_em >= sete_dias, Usuario.is_admin == False).count()

    # Top 5 usuários mais ativos
    top_usuarios = db.session.query(
        Usuario.nome, Usuario.email, db.func.count(Tarefa.id).label("total")
    ).join(Tarefa, isouter=True).filter(Usuario.is_admin == False).group_by(Usuario.id).order_by(db.text("total DESC")).limit(5).all()

    return jsonify({
        "total_usuarios":  total_usuarios,
        "usuarios_ativos": usuarios_ativos,
        "total_tarefas":   total_tarefas,
        "tarefas_feitas":  tarefas_feitas,
        "atrasadas":       atrasadas,
        "novos_7dias":     novos,
        "taxa_conclusao":  round(tarefas_feitas / total_tarefas * 100, 1) if total_tarefas else 0,
        "top_usuarios": [{"nome": n, "email": e, "total": t} for n, e, t in top_usuarios],
    })


# Listar usuários
@app.route("/admin/usuarios")
@login_required
@admin_required
def admin_usuarios():
    usuarios = Usuario.query.filter_by(is_admin=False).order_by(Usuario.criado_em.desc()).all()
    return jsonify([u.to_dict(admin=True) for u in usuarios])


# Tarefas de um usuário específico
@app.route("/admin/usuarios/<int:uid>/tarefas")
@login_required
@admin_required
def admin_tarefas_usuario(uid):
    u = db.session.get(Usuario, uid)
    if not u:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    tarefas = Tarefa.query.filter_by(usuario_id=uid).order_by(Tarefa.criada_em.desc()).all()
    return jsonify({"usuario": u.to_dict(admin=True), "tarefas": [t.to_dict() for t in tarefas]})


# Deletar usuário
@app.route("/admin/usuarios/<int:uid>/deletar", methods=["POST"])
@login_required
@admin_required
def admin_deletar_usuario(uid):
    u = db.session.get(Usuario, uid)
    if not u or u.is_admin:
        return jsonify({"erro": "Usuário não encontrado ou protegido"}), 404
    db.session.delete(u)
    db.session.commit()
    return jsonify({"ok": True})


# Ativar / desativar usuário
@app.route("/admin/usuarios/<int:uid>/toggle-ativo", methods=["POST"])
@login_required
@admin_required
def admin_toggle_ativo(uid):
    u = db.session.get(Usuario, uid)
    if not u or u.is_admin:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    u.ativo = not u.ativo
    db.session.commit()
    return jsonify({"ok": True, "ativo": u.ativo})


# Resetar senha de usuário
@app.route("/admin/usuarios/<int:uid>/reset-senha", methods=["POST"])
@login_required
@admin_required
def admin_reset_senha(uid):
    u = db.session.get(Usuario, uid)
    if not u or u.is_admin:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    nova = secrets.token_urlsafe(8)
    u.set_senha(nova)
    db.session.commit()
    return jsonify({"ok": True, "nova_senha": nova})


# ══════════════════════════════════════
#  TAREFAS
# ══════════════════════════════════════
@app.route("/tasks")
@login_required
def get_tasks():
    uid = session["usuario_id"]
    tarefas = Tarefa.query.filter_by(usuario_id=uid).order_by(Tarefa.ordem, Tarefa.criada_em.desc()).all()
    return jsonify([t.to_dict() for t in tarefas])


@app.route("/add", methods=["POST"])
@login_required
def add():
    data = request.get_json()
    if not data or not data.get("descricao", "").strip():
        return jsonify({"erro": "Descrição obrigatória"}), 400
    uid = session["usuario_id"]
    max_ordem = db.session.query(db.func.max(Tarefa.ordem)).filter_by(usuario_id=uid).scalar() or 0
    t = Tarefa(usuario_id=uid, descricao=data["descricao"].strip(),
        prazo=data.get("prazo") or None, hora=data.get("hora") or None,
        categoria=data.get("categoria") or None, prioridade=data.get("prioridade","media"),
        ordem=max_ordem+1)
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@app.route("/toggle", methods=["POST"])
@login_required
def toggle():
    data = request.get_json()
    t, err, status = tarefa_ou_404(data.get("id"))
    if err: return err, status
    t.concluida = 0 if t.concluida == 1 else 1
    db.session.commit()
    return jsonify({"id": t.id, "concluida": t.concluida})


@app.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete(id):
    t, err, status = tarefa_ou_404(id)
    if err: return err, status
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/edit", methods=["POST"])
@login_required
def edit():
    data = request.get_json()
    t, err, status = tarefa_ou_404(data.get("id"))
    if err: return err, status
    t.descricao  = data.get("descricao", t.descricao).strip()
    t.prazo      = data.get("prazo") or None
    t.hora       = data.get("hora") or None
    t.categoria  = data.get("categoria") or None
    t.prioridade = data.get("prioridade", t.prioridade)
    db.session.commit()
    return jsonify(t.to_dict())


@app.route("/reorder", methods=["POST"])
@login_required
def reorder():
    uid = session["usuario_id"]
    for i, tid in enumerate(request.get_json().get("ids", [])):
        t = Tarefa.query.filter_by(id=tid, usuario_id=uid).first()
        if t: t.ordem = i
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/buscar")
@login_required
def buscar():
    uid = session["usuario_id"]
    q   = request.args.get("q","").strip()
    if not q: return jsonify([])
    r = Tarefa.query.filter(Tarefa.usuario_id==uid,
        Tarefa.descricao.ilike(f"%{q}%") | Tarefa.categoria.ilike(f"%{q}%")).all()
    return jsonify([t.to_dict() for t in r])


@app.route("/stats")
@login_required
def stats():
    uid  = session["usuario_id"]
    hoje = datetime.now().strftime("%Y-%m-%d")
    base = Tarefa.query.filter_by(usuario_id=uid)
    total = base.count()
    conc  = base.filter_by(concluida=1).count()
    return jsonify({
        "total": total, "concluidas": conc,
        "pendentes": base.filter_by(concluida=0).count(),
        "atrasadas": base.filter(Tarefa.prazo<hoje, Tarefa.concluida==0).count(),
        "para_hoje": base.filter_by(prazo=hoje).count(),
        "taxa_conclusao": round(conc/total*100,1) if total else 0,
    })


@app.route("/export/csv")
@login_required
def export_csv():
    uid = session["usuario_id"]
    tarefas = Tarefa.query.filter_by(usuario_id=uid).all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID","Descrição","Prazo","Hora","Concluída","Categoria","Prioridade"])
    for t in tarefas:
        w.writerow([t.id, t.descricao, t.prazo or "", t.hora or "",
            "Sim" if t.concluida else "Não", t.categoria or "", t.prioridade])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment; filename=tarefas.csv"})


@app.route("/export/json")
@login_required
def export_json():
    uid = session["usuario_id"]
    tarefas = Tarefa.query.filter_by(usuario_id=uid).all()
    return Response(json.dumps([t.to_dict() for t in tarefas], ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition":"attachment; filename=tarefas.json"})


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data     = request.get_json()
    mensagem = (data.get("mensagem") or "").strip()
    if not mensagem: return jsonify({"resposta": "Pode me perguntar algo!"}), 400
    uid     = session["usuario_id"]
    tarefas = Tarefa.query.filter_by(usuario_id=uid).all()
    hoje    = datetime.now().strftime("%Y-%m-%d")
    resumo  = "\n".join(f"- [{t.prioridade.upper()}] {t.descricao} {'✅' if t.concluida else '⏳'}"
                        + (" ⚠️" if t.prazo and t.prazo < hoje and not t.concluida else "")
                        for t in tarefas) or "Sem tarefas."
    api_key = os.environ.get("ANTHROPIC_API_KEY","")
    if not api_key:
        return _chat_fallback(mensagem, tarefas, hoje)
    try:
        # Monta histórico de mensagens com contexto
        historico = data.get("historico", [])
        # Garante que só tem user/assistant alternados e sem duplicatas
        mensagens = []
        for h in historico[:-1]:  # remove a última (é a mensagem atual)
            if h.get("role") in ("user","assistant"):
                mensagens.append({"role": h["role"], "content": h["content"]})
        mensagens.append({"role": "user", "content": mensagem})

        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":api_key,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 350,
                "system": f"""Você é o Robo, assistente pessoal de produtividade do app To-Do Pro.
Personalidade: animado, direto, amigável e motivador. Fale em português brasileiro informal.
Respostas curtas (máx 3 frases). Sem markdown, sem listas, sem emojis em excesso.
Você conhece as tarefas do usuário e pode comentar sobre elas.

Lista de tarefas atual:
{resumo}

Data de hoje: {datetime.now().strftime('%d/%m/%Y (%A)')}
Hora atual: {datetime.now().strftime('%H:%M')}""",
                "messages": mensagens
            }, timeout=15)
        r.raise_for_status()
        return jsonify({"resposta": r.json()["content"][0]["text"]})
    except Exception as e:
        return _chat_fallback(mensagem, tarefas, hoje)


def _chat_fallback(msg, tarefas, hoje):
    import random
    m  = msg.lower()
    p  = [t for t in tarefas if not t.concluida]
    c  = [t for t in tarefas if t.concluida]
    at = [t for t in tarefas if t.prazo and t.prazo < hoje and not t.concluida]
    if any(x in m for x in ["quantas","total"]): return jsonify({"resposta":f"{len(tarefas)} tarefas: {len(p)} pendentes, {len(c)} feitas."})
    if any(x in m for x in ["atrasad"]): return jsonify({"resposta":f"{len(at)} atrasada(s)." if at else "Nenhuma atrasada!"})
    if any(x in m for x in ["urgente","priorit"]):
        a = next((t for t in p if t.prioridade=="alta"),None)
        return jsonify({"resposta":f"Mais urgente: '{a.descricao}'" if a else "Sem alta prioridade!"})
    return jsonify({"resposta":random.choice(["Me pergunta sobre suas tarefas!","Não entendi, tenta de novo."])})


@app.route("/clear-done", methods=["POST"])
@login_required
def clear_done():
    uid = session["usuario_id"]
    Tarefa.query.filter_by(usuario_id=uid, concluida=1).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/clear-all", methods=["POST"])
@login_required
def clear_all():
    uid = session["usuario_id"]
    Tarefa.query.filter_by(usuario_id=uid).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json()
    senha_atual = data.get("senha_atual") or ""
    nova_senha  = data.get("nova_senha") or ""

    if not senha_atual or not nova_senha:
        return jsonify({"erro": "Preencha todos os campos"}), 400
    if len(nova_senha) < 6:
        return jsonify({"erro": "Nova senha deve ter pelo menos 6 caracteres"}), 400

    u = usuario_atual()
    if not u.check_senha(senha_atual):
        return jsonify({"erro": "Senha atual incorreta"}), 401

    u.set_senha(nova_senha)
    db.session.commit()
    return jsonify({"ok": True})


@app.errorhandler(404)
def nao_encontrado(_):
    if request.is_json or request.path.startswith("/admin/") or request.path.startswith("/auth/"):
        return jsonify({"erro": "Não encontrado"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def erro_interno(_):
    if request.is_json or request.path.startswith("/admin/") or request.path.startswith("/auth/"):
        return jsonify({"erro": "Erro interno do servidor"}), 500
    return render_template("500.html"), 500


if __name__ == "__main__":
    app.run(debug=True)