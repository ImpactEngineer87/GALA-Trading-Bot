"""Preview/apply the v3.3.0 patch to a STOPPED v3.2.0 installation, with backups."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import yaml
from MIGRATE_CONFIG_V3_3_0 import migrate


def apply_upgrade(source, target, apply=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('Extract the upgrade to a separate directory first')
    if not (target/'server.py').is_file() or not (target/'config.yaml').is_file():
        raise ValueError('Target must be the existing bot directory containing server.py and config.yaml')
    version_path = target/'VERSION'
    if not version_path.is_file() or version_path.read_text().strip() not in ('3.2.0', '3.3.0'):
        raise ValueError('This upgrade supports the supplied v3.2.0 source and v3.3.0 only')
    manifest = json.loads((source/'V3_3_0_MANIFEST.json').read_text())
    names = []
    for row in manifest['files']:
        name = row['path']
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or rel.parts[0] in ('data', 'logs') or name in ('.env', 'config.yaml'):
            raise ValueError('Invalid patch manifest path')
        if hashlib.sha256((source/rel).read_bytes()).hexdigest() != row['sha256']:
            raise ValueError(f'Patch integrity check failed: {name}')
        names.append(name)
    with (target/'config.yaml').open(encoding='utf-8-sig') as handle:
        cfg = yaml.safe_load(handle) or {}
    updated = migrate(cfg)
    if not apply:
        return {'applied':False, 'files':names, 'config_migration':updated != cfg}
    backup = target/'backups'/('before_v3.3.0_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    backup.mkdir(parents=True)
    previous = {name:(target/name).exists() for name in names}
    for name, exists in previous.items():
        if exists:
            (backup/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target/name, backup/name)
    shutil.copy2(target/'config.yaml', backup/'config.yaml')
    installed = []
    try:
        for name in names:
            dest = target/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_suffix(dest.suffix + '.upgrade.tmp')
            shutil.copy2(source/name, temp)
            os.replace(temp, dest)
            installed.append(name)
        temp = target/'config.yaml.upgrade.tmp'
        with temp.open('w', encoding='utf-8') as handle:
            yaml.safe_dump(updated, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target/'config.yaml')
    except Exception:
        for name in reversed(installed):
            if previous[name]:
                shutil.copy2(backup/name, target/name)
            else:
                (target/name).unlink(missing_ok=True)
        shutil.copy2(backup/'config.yaml', target/'config.yaml')
        raise
    return {'applied':True, 'files':names, 'backup':str(backup),
            'preserved':['.env','data/','logs/','existing REAL selection and ticker limit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--apply', action='store_true', help='Write the patch; stop the bot first')
    args = parser.parse_args()
    result = apply_upgrade(Path(__file__).parent, args.target, args.apply)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
