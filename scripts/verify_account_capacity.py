"""Disposable functional capacity check; real low-cost salts plus separately timed production bcrypt."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from tempfile import TemporaryDirectory
from time import perf_counter
from pathlib import Path
from cryptography.fernet import Fernet
import bcrypt
from sqlalchemy import select,func
from app.config import Settings
from app.database import Base,create_engine_from_url,sessionmaker_for_engine
from app.models import User,AdminBatch
from app.security import hash_password
from app.api.account_management import BatchConfirm,ResetSelection
from app.services import account_batch_service as batches
from app.services.time_utils import utc_now
with TemporaryDirectory(prefix='dai-account-capacity-') as temp:
 settings=Settings(_env_file=None,account_security_keys=Fernet.generate_key().decode())
 engine=create_engine_from_url(f'sqlite:///{temp}/test.db');Base.metadata.create_all(engine);factory=sessionmaker_for_engine(engine)
 with factory() as db:
  admin=User(username='capacity-admin',real_name='Capacity',role='admin',status='active',password_hash=hash_password('CapacityTest123!'));db.add(admin);db.commit();aid=admin.id
 # Keep unique bcrypt salts and valid hashes, reducing cost only in this disposable verifier.
 batches.hash_password=lambda password:bcrypt.hashpw(password.encode(),bcrypt.gensalt(rounds=4)).decode()
 start=perf_counter()
 with factory() as db:
  actor=db.get(User,aid)
  batch=batches.create_import_preview(db,actor,'students',{},[(i+2,{'student_no':f'{i:08d}','real_name':f'Student {i}'}) for i in range(5000)],settings)
  ident=batch.id;batches.confirm_batch(db,ident,actor,BatchConfirm(preview_digest=batch.preview_digest),settings)
 with factory() as db:claim=batches.claim_batch(db)
 batches.process_batch(factory,*claim,settings)
 with factory() as db:
  assert db.get(AdminBatch,ident).status=='succeeded'
  assert db.scalar(select(func.count()).select_from(User).where(User.role=='student'))==5000
  assert db.scalar(select(func.count(func.distinct(User.password_hash))).where(User.role=='student'))==5000
 print(f'5000 student atomic import: PASS ({perf_counter()-start:.2f}s, test-only bcrypt cost 4)',flush=True)
 start=perf_counter()
 with factory() as db:
  actor=db.get(User,aid)
  batch=batches.create_reset_preview(db,actor,ResetSelection(all_matching=True),settings);ident=batch.id
  batches.confirm_batch(db,ident,actor,BatchConfirm(preview_digest=batch.preview_digest,current_password='CapacityTest123!',reason='capacity test'),settings)
 with factory() as db:claim=batches.claim_batch(db)
 batches.process_batch(factory,*claim,settings)
 with factory() as db:
  assert db.get(AdminBatch,ident).status=='succeeded'
  assert db.scalar(select(func.count()).select_from(User).where(User.role=='student',User.session_version==2))==5000
 print(f'5000 student reset: PASS ({perf_counter()-start:.2f}s, test-only bcrypt cost 4)',flush=True)
 start=perf_counter()
 with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(hash_password,['CapacityProductionSample!']*60))
 print(f'Production bcrypt cost 12, 60 distinct salts / 2 workers: {perf_counter()-start:.2f}s',flush=True)
