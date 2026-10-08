"""Registering a probed image never rewrites a historical environment version."""
import importlib.util
from pathlib import Path

from sqlalchemy import select
from app.models import EnvironmentProfile, EnvironmentVersion, ProfileVersionPackage
from conftest import create_user


def test_registration_is_idempotent_and_preserves_existing_versions(db_session_factory):
    path=Path(__file__).resolve().parents[3]/'scripts/register_lab_environment.py'
    spec=importlib.util.spec_from_file_location('lab_environment_registration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    admin=create_user(db_session_factory,'lab-env-admin','admin')
    report={'python':'3.12.13','packages':{name:'1.0.0' for name in module.PACKAGES}}
    report['packages']['torch']='2.14.0+cpu'
    with db_session_factory() as db:
        before={v.id:v.image_digest for v in db.scalars(select(EnvironmentVersion))}
        version=module.register(db,'sha256:'+'b'*64,report,'cpu',admin.id,'python@sha256:'+'a'*64)
        db.commit();ident=version.id
        again=module.register(db,'sha256:'+'b'*64,report,'cpu',admin.id,'python@sha256:'+'a'*64)
        assert again.id==ident
        profile=db.scalar(select(EnvironmentProfile).where(EnvironmentProfile.slug=='lab-pytorch-cpu'))
        assert profile.current_version_id==ident
        assert len(db.scalars(select(ProfileVersionPackage).where(ProfileVersionPackage.environment_version_id==ident)).all())==len(module.PACKAGES)
        assert all(db.get(EnvironmentVersion,key).image_digest==digest for key,digest in before.items())
