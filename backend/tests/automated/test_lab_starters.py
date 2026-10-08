"""Run the shipped teaching notebooks on a real patched CPU image."""
import csv
import io
import json
import os
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

from app.services.lab_data_service import register_dataset_upload, validate_dataset, materialize_dataset
from app.services.lab_grading import compute_metrics
from app.services.lab_processing import normalise_cells
from app.services.lab_runtime import execute_training, execute_prediction
from app.services.lab_starters import starter_template
from conftest import create_user
from test_lab_data_service import _png


@pytest.mark.parametrize('kind',['tabular_regression','tabular_classification','image_classification'])
def test_teaching_starter_trains_and_predicts_in_separate_sandbox(kind,db_session_factory,test_settings,tmp_path):
    image_name=os.getenv('DAI_LAB_TEST_TORCH_IMAGE')
    if not image_name:
        pytest.skip('Set DAI_LAB_TEST_TORCH_IMAGE for the real CPU teaching-template acceptance test')
    image=subprocess.run(['docker','image','inspect','--format','{{.Id}}',image_name],check=True,capture_output=True,text=True).stdout.strip()
    teacher=create_user(db_session_factory,'starter-teacher','teacher')
    settings=test_settings.model_copy(update={'judge_work_dir':str(tmp_path),'judge_host_work_dir':'','lab_min_free_disk_mb':64})
    if kind=='image_classification':
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            for i in range(100): archive.writestr(f'{"dark" if i<50 else "light"}/{i}.png',_png(i*2))
        stream.seek(0);filename='images.zip'
    else:
        text='sample_id,value,label\n'+''.join(f'{i},{i/20},{i/10 if kind=="tabular_regression" else i%2}\n' for i in range(100))
        stream=io.BytesIO(text.encode());filename='table.csv'
    with db_session_factory() as db:
        dataset=register_dataset_upload(db,teacher.id,kind,kind,{},filename,stream,settings)
        validate_dataset(db,dataset.id,settings);db.commit()
        assert dataset.status=='ready',dataset.error
        for split in ('train','validation'):
            materialize_dataset(db,dataset.id,split,tmp_path/'course'/split,settings,include_labels=True)
        materialize_dataset(db,dataset.id,'test',tmp_path/'test-input',settings)
        dataset_id=dataset.id;classes=dataset.summary['class_labels']
    template=starter_template(kind)
    for cell in template['cells']:
        if cell['id']=='parameters': cell['source']=cell['source'].replace('EPOCHS = 10','EPOCHS = 2')
    resources={'cpus':1,'memory_mb':4096,'disk_mb':128,'timeout_seconds':180}
    train=execute_training(run_id=992101,lease_token=str(uuid.uuid4()),image_ref=image,
        cells=normalise_cells(template['cells']),course_dir=tmp_path/'course',work_dir=tmp_path/'model',
        resources=resources,settings=settings)
    assert train.exit_code==0,train.logs
    assert (train.output_dir/'model.pt').is_file()
    assert len(json.loads((train.output_dir/'metrics.json').read_text()))==2
    prediction=execute_prediction(run_id=992102,lease_token=str(uuid.uuid4()),image_ref=image,
        artifact_dir=train.output_dir,input_dir=tmp_path/'test-input',output_dir=tmp_path/'predicted',
        resources=resources,settings=settings)
    assert prediction.exit_code==0,prediction.logs
    assert not (tmp_path/'test-input'/'labels.csv').exists()
    with db_session_factory() as db:
        materialize_dataset(db,dataset_id,'test',tmp_path/'scorer',settings,include_labels=True)
    metrics=compute_metrics(kind,tmp_path/'scorer'/'labels.csv',tmp_path/'predicted'/'predictions.csv',classes)
    assert ('rmse' if kind=='tabular_regression' else 'macro_f1') in metrics


def test_public_export_bundles_never_contain_test_partition(db_session_factory,test_settings,tmp_path):
    from sqlalchemy import select
    from app.models import LabFile, StorageObject
    from app.storage import StorageService,StorageArea
    teacher=create_user(db_session_factory,'export-teacher','teacher')
    content='sample_id,x,label\n'+''.join(f'{i},{i},{i%2}\n' for i in range(40))
    with db_session_factory() as db:
        dataset=register_dataset_upload(db,teacher.id,'CSV','tabular_classification',{},'samples.csv',io.BytesIO(content.encode()),test_settings)
        validate_dataset(db,dataset.id,test_settings);db.commit()
        assert dataset.status=='ready',dataset.error
        materialize_dataset(db,dataset.id,'test',tmp_path/'test',test_settings,include_labels=True)
        hidden_ids={r['sample_id'] for r in csv.DictReader((tmp_path/'test'/'samples.csv').open())}
        bundles=db.scalars(select(LabFile).where(LabFile.dataset_version_id==dataset.id,LabFile.role=='public_bundle')).all()
        assert {b.path for b in bundles}=={'downloads/train.zip','downloads/validation.zip'}
        for bundle in bundles:
            obj=db.get(StorageObject,bundle.object_id)
            data=StorageService.from_settings(test_settings).read(StorageArea.DATASETS,obj.object_key)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                assert set(archive.namelist())=={'samples.csv','labels.csv'}
                for name in archive.namelist():
                    ids={r['sample_id'] for r in csv.DictReader(io.StringIO(archive.read(name).decode()))}
                    assert not ids & hidden_ids
