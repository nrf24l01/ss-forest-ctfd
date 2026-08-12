import hmac
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import current_app

from CTFd.models import db

from .models import TerritoryOwlChallenge, TerritoryOwlInstance


def _setting(name, default=None):
    return current_app.config.get(name, os.environ.get(name, default))


def _runtime_root():
    root = Path(_setting("TERRITORY_OWL_RUNTIME_ROOT"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_source_dir(source_dir):
    path = Path(source_dir)
    if path.is_absolute() or ".." in path.parts or not source_dir:
        raise ValueError("source_dir must be a relative template directory")
    return path


def _project_name(team_id, challenge_id):
    return f"territoryowl_t{team_id}_c{challenge_id}"


def _instance_dir(project_name):
    return _runtime_root() / "run" / project_name


def _compose(command, project_name, directory):
    socket = _setting("TERRITORY_OWL_DOCKER_HOST", "unix:///var/run/docker.sock")
    result = subprocess.run(
        ["docker", "--host", socket, "compose", "--project-name", project_name, "--env-file", ".env", *command],
        cwd=directory,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def _allocate_port():
    first = int(_setting("TERRITORY_OWL_PORT_START", "42000"))
    last = int(_setting("TERRITORY_OWL_PORT_END", "42099"))
    used = {row.port for row in TerritoryOwlInstance.query.with_entities(TerritoryOwlInstance.port)}
    for port in range(first, last + 1):
        if port not in used:
            return port
    raise RuntimeError("no free territory_owl ports remain")


def _refresh_frpc():
    token = _setting("TERRITORY_OWL_FRP_TOKEN")
    if not token:
        raise RuntimeError("TERRITORY_OWL_FRP_TOKEN is not configured")

    lines = [
        "[common]",
        f"token = {token}",
        f"server_addr = {_setting('TERRITORY_OWL_FRPS_HOST', 'territory-owl-frps')}",
        f"server_port = {_setting('TERRITORY_OWL_FRPS_PORT', '7000')}",
        "admin_addr = 0.0.0.0",
        "admin_port = 7400",
    ]
    instances = TerritoryOwlInstance.query.join(TerritoryOwlChallenge).all()
    for instance in instances:
        challenge = TerritoryOwlChallenge.query.get(instance.challenge_id)
        lines.extend(
            [
                "",
                f"[territory_owl_{instance.id}]",
                "type = tcp",
                f"local_ip = {instance.project_name}-service-1",
                f"local_port = {challenge.redirect_port}",
                f"remote_port = {instance.port}",
                "use_compression = true",
            ]
        )

    admin = _setting("TERRITORY_OWL_FRPC_ADMIN", "http://territory-owl-frpc:7400")
    config = "\n".join(lines) + "\n"
    response = requests.put(f"{admin}/api/config", data=config, timeout=10)
    response.raise_for_status()
    response = requests.get(f"{admin}/api/reload", timeout=10)
    response.raise_for_status()


def instance_for(team_id, challenge_id):
    instance = TerritoryOwlInstance.query.filter_by(team_id=team_id, challenge_id=challenge_id).first()
    if instance is not None and instance.expires_at <= datetime.utcnow():
        destroy(instance)
        return None
    return instance


def expire_instances():
    expired = TerritoryOwlInstance.query.filter(TerritoryOwlInstance.expires_at <= datetime.utcnow()).all()
    for instance in expired:
        destroy(instance)


def launch(team_id, challenge):
    existing = instance_for(team_id, challenge.id)
    if existing:
        return existing

    active = TerritoryOwlInstance.query.filter_by(team_id=team_id).count()
    if active >= int(_setting("TERRITORY_OWL_MAX_INSTANCES_PER_TEAM", "1")):
        raise RuntimeError("only one active instance is allowed per team")

    source = _runtime_root() / "templates" / _safe_source_dir(challenge.source_dir)
    if not (source / "docker-compose.yml").is_file():
        raise RuntimeError(f"missing Owl template: {challenge.source_dir}")

    project_name = _project_name(team_id, challenge.id)
    target = _instance_dir(project_name)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    instance = TerritoryOwlInstance(
        team_id=team_id,
        challenge_id=challenge.id,
        project_name=project_name,
        port=_allocate_port(),
        flag=f"silCTF{{{secrets.token_urlsafe(24)}}}",
        expires_at=datetime.utcnow() + timedelta(seconds=int(_setting("TERRITORY_OWL_INSTANCE_TTL_SECONDS", "3600"))),
    )
    (target / ".env").write_text(f"FLAG={instance.flag}\n", encoding="utf-8")

    try:
        _compose(["up", "-d"], project_name, target)
        db.session.add(instance)
        db.session.commit()
        _refresh_frpc()
        return instance
    except Exception:
        try:
            _compose(["down", "--remove-orphans"], project_name, target)
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(target, ignore_errors=True)
        if instance.id is not None:
            db.session.delete(instance)
            db.session.commit()
        else:
            db.session.rollback()
        raise


def destroy(instance):
    target = _instance_dir(instance.project_name)
    try:
        if target.exists():
            _compose(["down", "--remove-orphans"], instance.project_name, target)
    finally:
        db.session.delete(instance)
        db.session.commit()
        shutil.rmtree(target, ignore_errors=True)
        _refresh_frpc()


def correct_submission(team_id, challenge_id, submission):
    instance = instance_for(team_id, challenge_id)
    return instance is not None and hmac.compare_digest(instance.flag, submission)
