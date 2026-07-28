from datetime import datetime
from decimal import Decimal
import uuid

from CTFd.models import Challenges, db


POINTS = db.Numeric(18, 4)


class TerritoryChallenge(Challenges):
    __tablename__ = "territory_control_challenges"
    id = db.Column(db.Integer, db.ForeignKey("challenges.id"), primary_key=True)
    attack_points = db.Column(POINTS, nullable=False, default=Decimal("0"))
    __mapper_args__ = {"polymorphic_identity": "territory"}


class TeamIdentity(db.Model):
    __tablename__ = "territory_control_team_identities"
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    color = db.Column(db.String(6), nullable=False)
    attack_points = db.Column(POINTS, nullable=False, default=Decimal("0"))


class Territory(db.Model):
    __tablename__ = "territory_control_territories"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.String(128), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    owner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    defense_points = db.Column(POINTS, nullable=False, default=Decimal("1"))
    score_amount = db.Column(POINTS, nullable=False, default=Decimal("0"))
    score_interval_seconds = db.Column(db.Integer, nullable=False, default=300)
    last_awarded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class CaptureSession(db.Model):
    __tablename__ = "territory_control_capture_sessions"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    territory_id = db.Column(db.Integer, db.ForeignKey("territory_control_territories.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    attack_points = db.Column(POINTS, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class DeviceCommand(db.Model):
    __tablename__ = "territory_control_device_commands"
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.String(128), nullable=False, index=True)
    command_type = db.Column(db.String(32), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    delivered_at = db.Column(db.DateTime, nullable=True)


class TerritoryAward(db.Model):
    __tablename__ = "territory_control_awards"
    id = db.Column(db.Integer, primary_key=True)
    territory_id = db.Column(db.Integer, db.ForeignKey("territory_control_territories.id"), nullable=False)
    awarded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ctfd_award_id = db.Column(db.Integer, db.ForeignKey("awards.id"), unique=True, nullable=False)
