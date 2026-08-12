from datetime import datetime
from decimal import Decimal

from CTFd.models import Challenges, db


POINTS = db.Numeric(18, 4)


class TerritoryOwlChallenge(Challenges):
    __tablename__ = "territory_owl_challenges"

    id = db.Column(db.Integer, db.ForeignKey("challenges.id"), primary_key=True)
    source_dir = db.Column(db.String(128), nullable=False)
    redirect_port = db.Column(db.Integer, nullable=False)
    attack_points = db.Column(POINTS, nullable=False, default=Decimal("0"))

    __mapper_args__ = {"polymorphic_identity": "territory_owl"}


class TerritoryOwlInstance(db.Model):
    __tablename__ = "territory_owl_instances"
    __table_args__ = (db.UniqueConstraint("team_id", "challenge_id", name="uq_territory_owl_instance"),)

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"), nullable=False)
    project_name = db.Column(db.String(128), nullable=False, unique=True)
    port = db.Column(db.Integer, nullable=False, unique=True)
    flag = db.Column(db.String(128), nullable=False, unique=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
