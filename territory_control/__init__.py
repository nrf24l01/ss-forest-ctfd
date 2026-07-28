from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import os

import qrcode
from flask import abort, jsonify, request, render_template_string, send_file
from sqlalchemy.exc import IntegrityError

from CTFd.models import db
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge
from CTFd.utils.user import get_current_team
from CTFd.utils.decorators import admins_only

from .models import CaptureSession, DeviceCommand, TeamIdentity, Territory, TerritoryChallenge


BLACK = "000000"
CAPTURE_WINDOW_SECONDS = 30


def points(value, field="value"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result.quantize(Decimal("0.0001"))


def configured_points(name, default):
    return points(os.getenv(name, default), name)


def team_color(team_id):
    # Stable high-contrast colors make a team recognizable without requiring CTFd theme changes.
    digest = sha256(str(team_id).encode()).hexdigest()
    return f"{int(digest[:6], 16) | 0x404040:06x}"


def identity_for(team_id, lock=False):
    query = TeamIdentity.query.filter_by(team_id=team_id)
    if lock:
        query = query.with_for_update()
    identity = query.first()
    if identity is None:
        identity = TeamIdentity(team_id=team_id, color=team_color(team_id))
        db.session.add(identity)
        db.session.flush()
    return identity


def current_team_or_403():
    team = get_current_team()
    if team is None:
        abort(403, "Join a team before attacking a territory")
    return team


def expire_session(session):
    if session.status == "pending" and session.expires_at <= datetime.utcnow():
        session.status = "expired"
        session.completed_at = datetime.utcnow()
        identity = identity_for(session.team_id, lock=True)
        identity.attack_points += session.attack_points


class TerritoryControlChallenge(CTFdStandardChallenge):
    id = "territory"
    name = "Territory Attack Points"
    challenge_model = TerritoryChallenge
    templates = {
        "create": "/plugins/territory_control/assets/challenge-create.html",
        "update": "/plugins/territory_control/assets/challenge-update.html",
        "view": "/plugins/territory_control/assets/challenge-view.html",
    }
    scripts = {}

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json() or {}
        attack_points = points(data.get("attack_points", 0), "attack_points")
        # Challenges remain worth zero in CTFd; only plugin balance receives points on solve.
        mutable = dict(data)
        mutable["value"] = 0
        mutable["attack_points"] = attack_points
        # BaseChallenge reads the request itself. Instantiate directly to avoid mutating Flask input.
        challenge = cls.challenge_model(**mutable)
        db.session.add(challenge)
        db.session.commit()
        return challenge

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data["attack_points"] = str(challenge.attack_points)
        return data

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json() or {}
        if "attack_points" in data:
            challenge.attack_points = points(data["attack_points"], "attack_points")
        # A challenge may never be changed into an official-score source.
        challenge.value = 0
        for key, value in data.items():
            if key not in {"attack_points", "value", "id", "type"}:
                setattr(challenge, key, value)
        db.session.commit()
        return challenge

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)
        if team:
            identity = identity_for(team.id, lock=True)
            identity.attack_points += challenge.attack_points
            db.session.commit()


def load(app):
    from CTFd.plugins import register_plugin_assets_directory

    app.db.create_all()
    CHALLENGE_CLASSES[TerritoryControlChallenge.id] = TerritoryControlChallenge
    register_plugin_assets_directory(app, base_path="/plugins/territory_control/assets/")

    @app.get("/api/v1/territory-control/me")
    def territory_me():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        return jsonify(uuid=identity.uuid, color=identity.color, attack_points=str(identity.attack_points))

    @app.get("/api/v1/territory-control/me/qr")
    def territory_qr():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        image = qrcode.make(identity.uuid)
        output = BytesIO()
        image.save(output, "PNG")
        output.seek(0)
        return send_file(output, mimetype="image/png", download_name="team-uuid.png")

    @app.get("/territory-control")
    def territory_player_page():
        team = current_team_or_403()
        identity = identity_for(team.id)
        db.session.commit()
        territories = Territory.query.order_by(Territory.name).all()
        return render_template_string(
            PLAYER_TEMPLATE,
            attack_points=str(identity.attack_points),
            team_uuid=identity.uuid,
            territories=territories,
        )

    @app.post("/api/v1/territory-control/attacks")
    def start_attack():
        team = current_team_or_403()
        data = request.get_json(silent=True) or request.form
        try:
            attack_points = points(data.get("attack_points"), "attack_points")
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if attack_points == 0:
            return jsonify(error="attack_points must be greater than zero"), 400
        territory_id = data.get("territory_id")
        territory = Territory.query.filter_by(id=territory_id).with_for_update().first()
        if territory is None:
            return jsonify(error="unknown territory"), 404
        # Expiring stale sessions here guarantees their reserved points are returned before a new attack.
        stale = CaptureSession.query.filter_by(territory_id=territory.id, status="pending").with_for_update().all()
        for session in stale:
            expire_session(session)
        if any(session.status == "pending" for session in stale):
            db.session.commit()
            return jsonify(error="territory is already being captured"), 409
        identity = identity_for(team.id, lock=True)
        if identity.attack_points < attack_points:
            db.session.rollback()
            return jsonify(error="insufficient attack points"), 409
        identity.attack_points -= attack_points
        session = CaptureSession(
            territory_id=territory.id,
            team_id=team.id,
            attack_points=attack_points,
            expires_at=datetime.utcnow() + timedelta(seconds=CAPTURE_WINDOW_SECONDS),
        )
        db.session.add(session)
        db.session.commit()
        # Persist the command so driver and CTFd can run in different processes/hosts.
        db.session.add(DeviceCommand(
            node_id=territory.node_id,
            command_type="start_scan",
            payload={
            "session_id": session.id,
            "expires_at": session.expires_at.isoformat(),
            },
        ))
        db.session.commit()
        return jsonify(session_id=session.id, node_id=territory.node_id, expires_at=session.expires_at.isoformat())

    @app.get("/api/v1/territory-control/device/commands")
    def device_commands():
        """Short-poll command channel for the standalone territory device driver."""
        secret = os.getenv("TERRITORY_DEVICE_SECRET")
        if not secret or request.headers.get("X-Territory-Secret") != secret:
            abort(403)
        node_id = request.args.get("node_id", "").strip()
        if not node_id:
            return jsonify(error="node_id is required"), 400
        command = DeviceCommand.query.filter_by(node_id=node_id, delivered_at=None).order_by(DeviceCommand.id).with_for_update().first()
        if command is None:
            return ("", 204)
        command.delivered_at = datetime.utcnow()
        db.session.commit()
        return jsonify(id=command.id, type=command.command_type, **command.payload)

    @app.post("/api/v1/territory-control/device/scans")
    def device_scan():
        """Trusted serial bridge submits the UUID scanned by a physical territory node."""
        secret = os.getenv("TERRITORY_DEVICE_SECRET")
        if not secret or request.headers.get("X-Territory-Secret") != secret:
            abort(403)
        data = request.get_json(silent=True) or {}
        territory = Territory.query.filter_by(node_id=data.get("node_id")).with_for_update().first()
        if territory is None:
            return jsonify(color=BLACK, result="unknown_territory"), 404
        session = CaptureSession.query.filter_by(territory_id=territory.id, status="pending").with_for_update().first()
        if session is None:
            return jsonify(color=BLACK, result="no_pending_attack")
        expire_session(session)
        if session.status != "pending":
            db.session.commit()
            return jsonify(color=BLACK, result="expired")
        scanned = TeamIdentity.query.filter_by(uuid=str(data.get("uuid", ""))).first()
        if scanned is None or scanned.team_id != session.team_id:
            # A scan from another team cannot spend or consume the attacker's reservation.
            return jsonify(color=BLACK, result="uuid_mismatch"), 403
        defense_multiplier = configured_points("TERRITORY_DEFENSE_MULTIPLIER", "1")
        attack_multiplier = configured_points("TERRITORY_ATTACK_MULTIPLIER", "2")
        remaining = (territory.defense_points * defense_multiplier) - (session.attack_points * attack_multiplier)
        if remaining > 0:
            territory.defense_points = remaining
            result = "defended"
            color = identity_for(territory.owner_team_id).color if territory.owner_team_id else BLACK
        elif remaining == 0:
            territory.owner_team_id = None
            territory.defense_points = Decimal("0")
            result = "neutralized"
            color = BLACK
        else:
            territory.owner_team_id = session.team_id
            territory.defense_points = abs(remaining)
            territory.last_awarded_at = datetime.utcnow()
            result = "captured"
            color = scanned.color
        session.status = "completed"
        session.completed_at = datetime.utcnow()
        db.session.commit()
        return jsonify(color=color, result=result, defense_points=str(territory.defense_points))

    @app.route("/admin/territory-control", methods=["GET", "POST"])
    @admins_only
    def territory_admin():
        if request.method == "POST":
            try:
                territory = Territory(
                    node_id=request.form["node_id"].strip(),
                    name=request.form["name"].strip(),
                    defense_points=points(request.form.get("defense_points", "1"), "defense_points"),
                    score_amount=points(request.form.get("score_amount", "0"), "score_amount"),
                    score_interval_seconds=int(request.form.get("score_interval_seconds", 300)),
                )
                if territory.defense_points <= 0 or territory.score_interval_seconds <= 0:
                    raise ValueError("defense and interval must be greater than zero")
                if territory.score_amount != territory.score_amount.to_integral_value():
                    raise ValueError("score amount must be a whole number because CTFd Awards only support integers")
                db.session.add(territory)
                db.session.commit()
            except (KeyError, ValueError, IntegrityError) as error:
                db.session.rollback()
                return render_template_string(ADMIN_TEMPLATE, territories=Territory.query.all(), error=str(error)), 400
        return render_template_string(ADMIN_TEMPLATE, territories=Territory.query.order_by(Territory.name).all(), error=None)


ADMIN_TEMPLATE = """<!doctype html><title>Territory Control</title>
<h1>Territory Control</h1>
{% if error %}<p style='color:#b00'>{{ error }}</p>{% endif %}
<form method='post'>
  <label>Name <input name='name' required></label>
  <label>Node ID <input name='node_id' required></label>
  <label>Defense <input name='defense_points' type='number' min='0.0001' step='0.0001' value='1' required></label>
  <label>Score amount <input name='score_amount' type='number' min='0' step='1' value='0' required></label>
  <label>Interval seconds <input name='score_interval_seconds' type='number' min='1' value='300' required></label>
  <button>Create territory</button>
</form>
<table><tr><th>Name</th><th>Node</th><th>Owner</th><th>Defense</th><th>Score / interval</th></tr>
{% for territory in territories %}<tr><td>{{ territory.name }}</td><td>{{ territory.node_id }}</td><td>{{ territory.owner_team_id or 'neutral' }}</td><td>{{ territory.defense_points }}</td><td>{{ territory.score_amount }} / {{ territory.score_interval_seconds }}s</td></tr>{% endfor %}
</table>"""


PLAYER_TEMPLATE = """<!doctype html>
<title>Territory Control</title>
<style>
body { max-width: 960px; margin: 2rem auto; font: 16px system-ui, sans-serif; color: #18222c; padding: 0 1rem; }
.score-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }
.card { border: 1px solid #d8dee5; border-radius: .75rem; padding: 1.25rem; }
.card h2 { font-size: .9rem; text-transform: uppercase; letter-spacing: .08em; margin: 0 0 .5rem; color: #5b6875; }
.points { font-size: 2.5rem; font-weight: 700; color: #0b6e4f; }
.muted { color: #5b6875; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
th, td { padding: .7rem; border-bottom: 1px solid #e5e9ed; text-align: left; }
input, button { padding: .5rem; font: inherit; } button { background: #0b6e4f; color: white; border: 0; border-radius: .3rem; cursor: pointer; }
#result { min-height: 1.3rem; margin-top: .75rem; }
@media (max-width: 600px) { .score-grid { grid-template-columns: 1fr; } table { font-size: .85rem; } }
</style>
<h1>Territory Control</h1>
<p class="muted">Attack Points are spent on territory attacks. They are separate from the CTFd scoreboard, which shows Final Score generated by captured territories.</p>
<div class="score-grid">
  <section class="card"><h2>Attack Points</h2><div class="points" id="attack-points">{{ attack_points }}</div><p class="muted">Spend these to capture territories.</p></section>
  <section class="card"><h2>Team QR Code</h2><img src="/api/v1/territory-control/me/qr" width="150" height="150" alt="Team QR code"><p class="muted">Show this code to the territory scanner.</p></section>
</div>
<h2>Territories</h2>
<table><thead><tr><th>Territory</th><th>Node</th><th>Owner</th><th>Defense</th><th>Attack</th></tr></thead><tbody>
{% for territory in territories %}<tr><td>{{ territory.name }}</td><td>{{ territory.node_id }}</td><td>{{ territory.owner_team_id or 'Neutral' }}</td><td>{{ territory.defense_points }}</td><td><form class="attack-form" data-territory="{{ territory.id }}"><input name="attack_points" type="number" min="0.0001" step="0.0001" placeholder="AP" required><button>Start attack</button></form></td></tr>{% endfor %}
</tbody></table><p id="result"></p>
<script>
for (const form of document.querySelectorAll('.attack-form')) form.addEventListener('submit', async event => {
  event.preventDefault();
  const result = document.querySelector('#result');
  const response = await fetch('/api/v1/territory-control/attacks', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({territory_id: form.dataset.territory, attack_points: form.attack_points.value}) });
  const data = await response.json();
  result.textContent = response.ok ? `Attack started. Scan your team QR code within 30 seconds.` : (data.error || 'Attack could not start.');
  if (response.ok) document.querySelector('#attack-points').textContent = 'Reserved; refresh after the result.';
});
</script>"""
