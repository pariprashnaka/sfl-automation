"""Queue page (placeholder until stage 4)."""
from .routes import bp, page, flash


@bp.route("/marketing/queue")
def queue_page():
    return page("/marketing/queue", flash() + "<div class='card'>Queue arrives in stage 4.</div>", "Queue")
