import os
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, jsonify

from CTFd.exceptions.challenges import ChallengeCreateException, ChallengeUpdateException
from CTFd.models import Solves, db
from CTFd.plugins import register_plugin_assets_directory, register_plugin_script
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge, ChallengeResponse
from CTFd.utils.decorators import authed_only
from CTFd.utils.user import get_current_team

from . import instance_manager
from .models import TerritoryOwlChallenge, TerritoryOwlInstance


def award_attack_points(team_id, solve_id, attack_points):
    """Credit an Owl solve once while CTFd continues to award native score."""
    from CTFd.plugins.territory_control.models import TeamIdentity, TerritoryPointAward

    if TerritoryPointAward.query.get(solve_id) is not None:
        return
    identity = TeamIdentity.query.filter_by(team_id=team_id).with_for_update().first()
    if identity is None:
        from hashlib import sha256
        import uuid
        identity = TeamIdentity(
            team_id=team_id,
            uuid=str(uuid.uuid4()),
            color=f"{int(sha256(str(team_id).encode()).hexdigest()[:6], 16) | 0x404040:06x}",
        )
        db.session.add(identity)
        db.session.flush()
    identity.attack_points += attack_points
    db.session.add(TerritoryPointAward(solve_id=solve_id, team_id=team_id, attack_points=attack_points))


def _public_host(app):
    return app.config.get("TERRITORY_OWL_PUBLIC_HOST") or os.getenv("TERRITORY_OWL_PUBLIC_HOST", "127.0.0.1")


def _points(value):
    try:
        result = Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("attack_points must be a number")
    if result < 0:
        raise ValueError("attack_points cannot be negative")
    return result


def _challenge_data(request):
    data = dict(request.form or request.get_json() or {})
    try:
        data["attack_points"] = _points(data.get("attack_points", 0))
        data["redirect_port"] = int(data["redirect_port"])
        if not 1 <= data["redirect_port"] <= 65535:
            raise ValueError("redirect_port must be between 1 and 65535")
        instance_manager._safe_source_dir(data["source_dir"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChallengeCreateException(str(exc))
    data["value"] = int(data["attack_points"])
    data["max_attempts"] = data.get("max_attempts") or 0
    return data


class TerritoryOwlChallengeType(CTFdStandardChallenge):
    id = "territory_owl"
    name = "Инстанс территории"
    challenge_model = TerritoryOwlChallenge
    templates = {
        "create": "/plugins/territory_owl/assets/challenge-create.html",
        "update": "/plugins/territory_owl/assets/challenge-update.html",
        "view": "/plugins/territory_owl/assets/challenge-view.html",
    }
    scripts = {
        "create": "/plugins/territory_owl/assets/challenge-form.js",
        "update": "/plugins/territory_owl/assets/challenge-form.js",
        "view": "/plugins/territory_owl/assets/challenge-view.js?v=4",
    }

    @classmethod
    def create(cls, request):
        challenge = cls.challenge_model(**_challenge_data(request))
        db.session.add(challenge)
        db.session.commit()
        return challenge

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data.update(
            source_dir=challenge.source_dir,
            redirect_port=challenge.redirect_port,
            attack_points=str(challenge.attack_points),
        )
        return data

    @classmethod
    def update(cls, challenge, request):
        data = _challenge_data(request)
        for key, value in data.items():
            if key not in {"id", "type"}:
                setattr(challenge, key, value)
        db.session.commit()
        return challenge

    @classmethod
    def attempt(cls, challenge, request):
        team = get_current_team()
        if team is None:
            return ChallengeResponse(status="incorrect", message="Вступите в команду перед запуском инстанса")
        submission = (request.form or request.get_json() or {}).get("submission", "").strip()
        if instance_manager.correct_submission(team.id, challenge.id, submission):
            return ChallengeResponse(status="correct", message="Верный динамический флаг")
        return ChallengeResponse(status="incorrect", message="Неверный флаг или инстанс не запущен")

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)
        solve = Solves.query.filter_by(challenge_id=challenge.id, team_id=team.id).order_by(Solves.id.desc()).first()
        if solve is not None:
            award_attack_points(team.id, solve.id, challenge.attack_points)
            db.session.commit()

    @classmethod
    def delete(cls, challenge):
        for instance in TerritoryOwlInstance.query.filter_by(challenge_id=challenge.id).all():
            instance_manager.destroy(instance)
        super().delete(challenge)


def load(app):
    app.db.create_all()
    CHALLENGE_CLASSES[TerritoryOwlChallengeType.id] = TerritoryOwlChallengeType
    register_plugin_assets_directory(app, base_path="/plugins/territory_owl/assets/")
    register_plugin_script("/plugins/territory_owl/assets/board.js?v=2")

    blueprint = Blueprint("territory_owl", __name__, url_prefix="/plugins/territory_owl")

    @blueprint.route("/instances/<int:challenge_id>", methods=["GET"])
    @authed_only
    def get_instance(challenge_id):
        team = get_current_team()
        if team is None:
            abort(403, "Join a team before launching an instance")
        instance = instance_manager.instance_for(team.id, challenge_id)
        if instance is None:
            return jsonify(active=False)
        host = _public_host(app)
        return jsonify(active=True, host=host, port=instance.port)

    @blueprint.get("/challenge-rewards")
    def challenge_rewards():
        return jsonify({challenge.id: str(challenge.attack_points) for challenge in TerritoryOwlChallenge.query.all()})

    @blueprint.route("/instances/<int:challenge_id>", methods=["POST"])
    @authed_only
    def launch_instance(challenge_id):
        team = get_current_team()
        if team is None:
            abort(403, "Join a team before launching an instance")
        challenge = TerritoryOwlChallenge.query.get_or_404(challenge_id)
        try:
            instance = instance_manager.launch(team.id, challenge)
        except Exception as exc:
            return jsonify(success=False, message=str(exc)), 500
        host = _public_host(app)
        return jsonify(success=True, host=host, port=instance.port)

    @blueprint.route("/instances/<int:challenge_id>", methods=["DELETE"])
    @authed_only
    def destroy_instance(challenge_id):
        team = get_current_team()
        if team is None:
            abort(403, "Join a team before destroying an instance")
        instance = instance_manager.instance_for(team.id, challenge_id)
        if instance is not None:
            instance_manager.destroy(instance)
        return jsonify(success=True)

    app.register_blueprint(blueprint)
