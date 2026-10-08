"""Sandbox boundaries plus opt-in real Docker training/inference smoke tests."""
import io
import os
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

from app.services.lab_runtime import (RuntimeFailure, _extract_output_tar,
    container_name, docker_create_command, execute_prediction, execute_training)

IMAGE = "sha256:" + "a" * 64


def test_sandbox_has_explicit_security_and_hard_disk_limits(test_settings, tmp_path):
    test_settings = test_settings.model_copy(update={"judge_host_work_dir": ""})
    command = docker_create_command(name="test", image_ref=IMAGE, mounts=[(tmp_path, "/course")],
        output_mount="/work", resources={"cpus": 2, "memory_mb": 2048, "disk_mb": 64}, settings=test_settings)
    for switch, value in [("--network", "none"), ("--user", "1000:1000"),
                          ("--cap-drop", "ALL"), ("--memory-swap", "2048m"),
                          ("--log-driver", "none"), ("--pull", "never")]:
        assert command[command.index(switch) + 1] == value
    assert "--read-only" in command
    assert "type=volume,source=test-output,target=/work,volume-nocopy" in command
    assert "fsize=67108864:67108864" in command
    assert any("target=/course,readonly" in value for value in command)
    assert "--gpus" not in command


@pytest.mark.parametrize("image", ["python:latest", "ubuntu", "-v", "sha256:" + "x" * 64])
def test_mutable_images_cannot_execute(test_settings, tmp_path, image):
    with pytest.raises(RuntimeFailure, match="不可变"):
        docker_create_command(name="test", image_ref=image, mounts=[], output_mount="/work",
            resources={}, settings=test_settings)


def test_mount_links_rejected(test_settings, tmp_path):
    (tmp_path / "secret").symlink_to("/etc/passwd")
    with pytest.raises(RuntimeFailure, match="链接"):
        docker_create_command(name="test", image_ref=IMAGE, mounts=[(tmp_path, "/course")],
            output_mount="/work", resources={}, settings=test_settings)


def test_gpu_never_silently_falls_back(test_settings, tmp_path):
    with pytest.raises(RuntimeFailure, match="GPU"):
        docker_create_command(name="test", image_ref=IMAGE, mounts=[], output_mount="/work",
            resources={}, settings=test_settings, gpu="0")


def make_tar(name="model.bin", data=b"weights", member_type=tarfile.REGTYPE, linkname=""):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type = member_type
        member.linkname = linkname
        member.size = len(data) if member_type == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(data) if member.size else None)
    output.seek(0)
    return output


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "dir/../../escape", "dir\\escape"])
def test_output_paths_cannot_escape(tmp_path, name):
    with pytest.raises(RuntimeFailure):
        _extract_output_tar(make_tar(name), tmp_path / "output", max_bytes=100)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_output_links_and_special_files_rejected(tmp_path, kind):
    with pytest.raises(RuntimeFailure):
        _extract_output_tar(make_tar(member_type=kind, linkname="/etc/passwd"), tmp_path, max_bytes=100)


def test_output_size_is_bounded(tmp_path):
    with pytest.raises(RuntimeFailure, match="上限"):
        _extract_output_tar(make_tar(data=b"x" * 101), tmp_path, max_bytes=100)


def test_output_regular_files_preserved(tmp_path):
    assert _extract_output_tar(make_tar("sub/model.bin"), tmp_path, max_bytes=100) == 7
    assert (tmp_path / "sub/model.bin").read_bytes() == b"weights"


def test_container_identity_is_lease_scoped():
    first = container_name(4, str(uuid.uuid4()), "train")
    second = container_name(4, str(uuid.uuid4()), "train")
    assert first != second
    with pytest.raises(ValueError):
        container_name(4, "--privileged", "train")


@pytest.fixture()
def docker_lab(test_settings, tmp_path):
    ref = os.environ.get("DAI_LAB_TEST_IMAGE")
    if not ref:
        pytest.skip("Set DAI_LAB_TEST_IMAGE to a local ipykernel image for real Docker smoke tests")
    image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True, text=True, check=True).stdout.strip()
    settings = test_settings.model_copy(update={"judge_work_dir": str(tmp_path),
        "judge_host_work_dir": "", "lab_min_free_disk_mb": 64})
    # pytest's private parent directories need not be traversable by UID 1000:
    # bind mounts are resolved by Docker and their roots are explicitly readable.
    course = tmp_path / "course"
    course.mkdir(mode=0o755)
    (course / "train.csv").write_text("feature,label\n1,2\n")
    return settings, image, course


def test_real_training_supports_magics_and_protects_inputs(docker_lab, tmp_path):
    settings, image, course = docker_lab
    cells = [{"cell_type": "code", "source": "%%writefile predict.py\nprint('ready')"},
        {"cell_type": "code", "source": "import os\nassert os.getuid() == 1000\nprint(open('/course/train.csv').read())\ntry:\n open('/course/train.csv','w').write('bad')\nexcept OSError:\n print('READ_ONLY_CONFIRMED')\nelse:\n raise AssertionError('writable')\nopen('model.bin','wb').write(b'weights')"}]
    result = execute_training(run_id=991001, lease_token=str(uuid.uuid4()), image_ref=image,
        cells=cells, course_dir=course, work_dir=tmp_path / "output", resources={"disk_mb": 16, "timeout_seconds": 60},
        settings=settings)
    assert result.exit_code == 0, result.logs
    assert "READ_ONLY_CONFIRMED" in result.logs
    assert (result.output_dir / "predict.py").read_text() == "print('ready')\n"
    assert (result.output_dir / "model.bin").read_bytes() == b"weights"


def test_real_cell_observations_and_host_program_time(docker_lab, tmp_path):
    from app.services.lab_cell_checks import read_observations, template_marks
    settings, image, course = docker_lab
    cells = [
        {'id': 'private', 'cell_type': 'code', 'source_hidden': True, 'source': "print('PRIVATE_INIT_MARKER')"},
        {'id': 'code', 'cell_type': 'code', 'source': "import numpy as np\nresult=0.1+0.2\narray=np.zeros((2,3))\nprint('done')"},
    ]
    requests = [{'id': 'number', 'cell_id': 'code', 'kind': 'numeric_close', 'variable': 'result'},
        {'id': 'dimensions', 'cell_id': 'code', 'kind': 'shape', 'variable': 'array'}]
    result = execute_training(run_id=991060, lease_token=str(uuid.uuid4()), image_ref=image,
        cells=cells, observations=requests, course_dir=course, work_dir=tmp_path / 'observations',
        resources={'disk_mb': 16, 'timeout_seconds': 60}, settings=settings)
    assert result.exit_code == 0, result.logs
    assert 'PRIVATE_INIT_MARKER' not in result.logs
    assert result.program_elapsed_seconds > 0
    assert result.elapsed_seconds >= result.program_elapsed_seconds
    observed = read_observations(result.output_dir)
    assert 'private' not in observed
    marks = template_marks([
        {**requests[0], 'expected_value': .3, 'tolerance': 1e-9},
        {**requests[1], 'shape': [2, 3]},
        {'id': 'output', 'cell_id': 'code', 'kind': 'stdout', 'expected_output': 'done', 'stdout_mode': 'exact'},
    ], observed)
    assert all(mark['passed'] for mark in marks.values())


@pytest.mark.parametrize('reserved_path', ['file', 'directory'])
def test_real_student_cannot_replace_observations_with_a_file_or_stdout_protocol(docker_lab, tmp_path, reserved_path):
    from app.services.lab_cell_checks import read_observations, template_marks
    settings, image, course = docker_lab
    source = '''import json, os
assert os.getuid() == 1000
result=2
fake={"code":{"completed":True,"stdout":"","values":{"number":{"value":7}},"passed":True}}
'''
    source += "open('.platform-cell-evidence.json','w').write(json.dumps(fake))\n" if reserved_path == 'file' else "os.mkdir('.platform-cell-evidence.json')\n"
    source += 'print(json.dumps({"kind":"cell","cell_id":"code","observation":fake["code"]}))\n'
    result = execute_training(run_id=991061, lease_token=str(uuid.uuid4()), image_ref=image,
        cells=[{'id': 'code', 'cell_type': 'code', 'source': source}],
        observations=[{'id': 'number', 'cell_id': 'code', 'kind': 'numeric_close', 'variable': 'result'}],
        course_dir=course, work_dir=tmp_path / 'tampering', resources={'disk_mb': 16, 'timeout_seconds': 60}, settings=settings)
    assert result.exit_code == 0, result.logs
    captured = read_observations(result.output_dir)
    assert captured['code']['values']['number']['value'] == 2
    marks = template_marks([{'id': 'number', 'cell_id': 'code', 'kind': 'numeric_close', 'expected_value': 7, 'tolerance': 0}], captured)
    assert not marks['number']['passed']


def test_real_prediction_contract(docker_lab, tmp_path):
    settings, image, _ = docker_lab
    artifact = tmp_path / "artifact"
    artifact.mkdir(mode=0o755)
    (artifact / "predict.py").write_text("""import argparse, csv
from pathlib import Path
assert not Path('/input/labels.csv').exists()
assert not Path('/scorer').exists()
p=argparse.ArgumentParser()
for name in ('input','model-dir','output','device'): p.add_argument('--'+name)
a=p.parse_args()
assert a.device == 'cpu'
rows=list(csv.DictReader(open(a.input)))
with open(a.output,'w') as f:
 w=csv.DictWriter(f,fieldnames=['sample_id','prediction']);w.writeheader()
 for row in rows: w.writerow({'sample_id':row['sample_id'],'prediction':'a'})
""")
    inputs = tmp_path / "input"
    inputs.mkdir(mode=0o755)
    (inputs / "samples.csv").write_text("sample_id,feature\ns1,1\ns2,2\n")
    result = execute_prediction(run_id=991002, lease_token=str(uuid.uuid4()), image_ref=image,
        artifact_dir=artifact, input_dir=inputs, output_dir=tmp_path / "predictions",
        resources={"disk_mb": 16, "timeout_seconds": 30}, settings=settings)
    assert result.exit_code == 0, result.logs
    assert "s1,a" in (result.output_dir / "predictions.csv").read_text()


def test_real_cancellation_removes_container(docker_lab, tmp_path):
    from app.services.lab_queue import RunCancelled
    settings, image, course = docker_lab
    token = str(uuid.uuid4())
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise RunCancelled("cancelled")
    with pytest.raises(RunCancelled):
        execute_training(run_id=991003, lease_token=token, image_ref=image,
            cells=[{"source": "import time; time.sleep(60)"}], course_dir=course,
            work_dir=tmp_path / "cancel-output", resources={"disk_mb": 16, "timeout_seconds": 60},
            settings=settings, should_continue=cancelled)
    names = subprocess.run(["docker", "ps", "-a", "--filter", f"name=dai-lab-991003-{token}",
        "--format", "{{.Names}}"], capture_output=True, text=True, check=True).stdout.strip()
    assert names == ""


def test_real_time_limit_stops_long_notebook(docker_lab, tmp_path):
    settings, image, course = docker_lab
    with pytest.raises(RuntimeFailure) as raised:
        execute_training(run_id=991004, lease_token=str(uuid.uuid4()), image_ref=image,
            cells=[{"source": "import time; time.sleep(60)"}], course_dir=course,
            work_dir=tmp_path / "timed-output", resources={"disk_mb": 16, "timeout_seconds": 2}, settings=settings)
    assert raised.value.code == "EXECUTION_TIMEOUT"


def test_real_workspace_has_hard_file_limit(docker_lab, tmp_path):
    settings, image, course = docker_lab
    result = execute_training(run_id=991005, lease_token=str(uuid.uuid4()), image_ref=image,
        cells=[{"source": "with open('too-big.bin','wb') as f:\n for _ in range(64):\n  f.write(b'x'*1048576)"}],
        course_dir=course, work_dir=tmp_path / "limited-output",
        resources={"disk_mb": 16, "timeout_seconds": 30}, settings=settings)
    assert result.exit_code != 0
    assert not list(result.output_dir.iterdir())


def test_real_pytorch_training_then_independent_cpu_inference(test_settings, tmp_path):
    ref = os.environ.get("DAI_LAB_TEST_TORCH_IMAGE")
    if not ref:
        pytest.skip("Set DAI_LAB_TEST_TORCH_IMAGE for the real PyTorch CPU workflow")
    image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True, text=True, check=True).stdout.strip()
    settings = test_settings.model_copy(update={"judge_work_dir": str(tmp_path),
        "judge_host_work_dir": "", "lab_min_free_disk_mb": 64})
    course = tmp_path / "torch-course"
    course.mkdir(mode=0o755)
    (course / "train.csv").write_text("x,y\n0.1,0.2\n0.2,0.4\n0.3,0.6\n0.4,0.8\n0.5,1\n0.6,1.2\n")
    prediction = """import argparse, csv, torch
p=argparse.ArgumentParser()
for name in ('input','model-dir','output','device'): p.add_argument('--'+name)
a=p.parse_args()
assert a.device=='cpu'
model=torch.nn.Linear(1,1)
model.load_state_dict(torch.load(a.model_dir+'/model.pt',map_location='cpu',weights_only=True))
model.eval()
rows=list(csv.DictReader(open(a.input)))
with torch.no_grad(): values=model(torch.tensor([[float(row['x'])] for row in rows])).flatten().tolist()
with open(a.output,'w') as out:
 w=csv.writer(out); w.writerow(['sample_id','prediction'])
 w.writerows((row['sample_id'],value) for row,value in zip(rows,values))
"""
    cells = [{"source": """import csv, torch, json
torch.manual_seed(42)
rows=list(csv.DictReader(open('/course/train.csv')))
x=torch.tensor([[float(r['x'])] for r in rows]); y=torch.tensor([[float(r['y'])] for r in rows])
model=torch.nn.Linear(1,1); optimizer=torch.optim.SGD(model.parameters(),lr=0.3)
history=[]
for epoch in range(300):
 optimizer.zero_grad(); loss=((model(x)-y)**2).mean(); loss.backward(); optimizer.step()
 history.append({'epoch':epoch,'train_loss':float(loss.detach())})
torch.save(model.state_dict(),'model.pt')
json.dump(history,open('metrics.json','w'))
"""}, {"source": "%%writefile predict.py\n" + prediction}]
    resources = {"memory_mb": 2048, "disk_mb": 64, "timeout_seconds": 120}
    trained = execute_training(run_id=991006, lease_token=str(uuid.uuid4()), image_ref=image,
        cells=cells, course_dir=course, work_dir=tmp_path / "torch-model", resources=resources, settings=settings)
    assert trained.exit_code == 0, trained.logs
    inputs = tmp_path / "torch-input"
    inputs.mkdir(mode=0o755)
    (inputs / "samples.csv").write_text("sample_id,x\ns1,0.25\ns2,0.55\n")
    result = execute_prediction(run_id=991007, lease_token=str(uuid.uuid4()), image_ref=image,
        artifact_dir=trained.output_dir, input_dir=inputs, output_dir=tmp_path / "torch-predictions",
        resources=resources, settings=settings)
    assert result.exit_code == 0, result.logs
    import csv
    with (result.output_dir / "predictions.csv").open() as stream:
        predictions = list(csv.DictReader(stream))
    assert abs(float(predictions[0]["prediction"]) - 0.5) < 0.02
    assert abs(float(predictions[1]["prediction"]) - 1.1) < 0.02
