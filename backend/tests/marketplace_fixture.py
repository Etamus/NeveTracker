"""Keep simulated access blocks and validation metadata out of real profiles."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def isolate_marketplace_control(test):
    folder=TemporaryDirectory();test.addCleanup(folder.cleanup)
    root=Path(folder.name)
    for target in [
        patch('services.marketplace_intervention._path',side_effect=lambda store,kind:root/f'{store}-{kind}.json'),
        patch('services.marketplace_session.metadata_file',side_effect=lambda store:root/f'{store}-validation.json'),
        patch('services.marketplace_access.path',side_effect=lambda store:root/f'{store}-backoff.json'),
    ]:
        target.start();test.addCleanup(target.stop)
