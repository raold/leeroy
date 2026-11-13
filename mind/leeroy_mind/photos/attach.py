"""Enable delivery only after all three opt-ins have been resolved at startup."""
from leeroy_mind.logs import log
from leeroy_mind.nightly.romance import set_pictures_available
from .config import load_photos_config
from .send import PhotoSender


def attach_photos(app):
    set_pictures_available(False)
    config = load_photos_config(app.data_dir / 'mind.toml')
    if not config.send:
        return None
    needed = ('selfies.enabled' if not app.selfies.cfg.enabled else
              'selfies.snaps' if not app.selfies.cfg.snaps else
              'server.enabled' if app.server is None else None)
    if needed:
        log('photos_send_needs', key=needed)
        return None
    sender = PhotoSender(app, config, app.rng)
    app.engine.attach_photos(sender.offer)
    app.engine.photos_send = True
    set_pictures_available(True)
    return sender
