"""Run periodic territory-score awards in a separate process/container."""
from datetime import datetime
import time

from CTFd import create_app
from CTFd.models import Awards, Teams, db

from .models import CaptureSession, Territory, TerritoryAward


def award_due_territories():
    now = datetime.utcnow()
    # Settling here makes the 30-second reservation deadline independent of API traffic.
    expired_sessions = CaptureSession.query.filter(
        CaptureSession.status == "pending", CaptureSession.expires_at <= now
    ).with_for_update().all()
    for session in expired_sessions:
        from . import expire_session
        expire_session(session)
    territories = Territory.query.filter(Territory.owner_team_id.isnot(None)).with_for_update().all()
    for territory in territories:
        if territory.score_amount <= 0:
            continue
        award_anchor = territory.last_awarded_at or territory.captured_at
        if award_anchor and (now - award_anchor).total_seconds() < territory.score_interval_seconds:
            continue
        team = Teams.query.filter_by(id=territory.owner_team_id).first()
        recipient_id = team.captain_id if team else None
        if recipient_id is None and team and team.members:
            recipient_id = team.members[0].id
        if recipient_id is None:
            # A deleted/empty team cannot receive a CTFd scoreboard award.
            continue
        award = Awards(
            user_id=recipient_id,
            team_id=territory.owner_team_id,
            name=f"Territory: {territory.name}",
            description=f"Periodic score from territory {territory.node_id}",
            value=territory.score_amount,
        )
        db.session.add(award)
        db.session.flush()
        db.session.add(TerritoryAward(territory_id=territory.id, ctfd_award_id=award.id))
        territory.last_awarded_at = now
    db.session.commit()


def main():
    app = create_app()
    while True:
        with app.app_context():
            award_due_territories()
        time.sleep(5)


if __name__ == "__main__":
    main()
