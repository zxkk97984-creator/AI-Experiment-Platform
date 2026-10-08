"""Small, inspectable teaching templates using the same public dataset contract."""
from app.schemas.lab import LabAssessmentConfig

MODEL_CODE = '''import torch
from torch import nn

def build_model(task, features, outputs):
    if task == "image_classification":
        return nn.Sequential(nn.Conv2d(1, 8, 3, padding=1), nn.ReLU(),
            nn.MaxPool2d(2), nn.Conv2d(8, 16, 3, padding=1), nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, 1)), nn.Flatten(), nn.Linear(16, outputs))
    return nn.Sequential(nn.Linear(features, 32), nn.ReLU(), nn.Linear(32, outputs))
'''

TRAIN_CODE = '''import json, random
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader, TensorDataset
from model import build_model

random.seed(SEED); np.random.seed(SEED); torch.manual_seed(SEED)
torch.set_num_threads(1)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def table(split):
    root = Path("/course") / split
    samples = pd.read_csv(root / "samples.csv", dtype={"sample_id": str})
    labels = pd.read_csv(root / "labels.csv", dtype={"sample_id": str, "label": str})
    return samples.merge(labels, on="sample_id", validate="one_to_one")

train, valid = table("train"), table("validation")
classification = TASK != "tabular_regression"
classes = sorted(train.label.unique().tolist()) if classification else []
class_to_index = {label: index for index, label in enumerate(classes)}
meta = {"task": TASK, "classes": classes, "seed": SEED, "features": 0}

if TASK == "image_classification":
    from PIL import Image
    class Images(Dataset):
        def __init__(self, frame, split): self.frame, self.root = frame, Path("/course") / split
        def __len__(self): return len(self.frame)
        def __getitem__(self, index):
            row = self.frame.iloc[index]
            with Image.open(self.root / row.image_path) as image:
                array = np.asarray(image.convert("L").resize((32, 32)), dtype=np.float32).copy() / 255
            return torch.from_numpy(array).unsqueeze(0), class_to_index[row.label]
    train_data, valid_data = Images(train, "train"), Images(valid, "validation")
else:
    features = train.drop(columns=["sample_id", "label"])
    encoded = pd.get_dummies(features, dtype=float).fillna(0)
    columns = encoded.columns.tolist()
    mean, scale = encoded.mean().to_numpy(), encoded.std(ddof=0).replace(0, 1).to_numpy()
    meta.update(columns=columns, mean=mean.tolist(), scale=scale.tolist(), features=len(columns))
    def x(frame):
        values = pd.get_dummies(frame.drop(columns=["sample_id", "label"]), dtype=float).reindex(columns=columns, fill_value=0).fillna(0).to_numpy()
        return torch.tensor((values - mean) / scale, dtype=torch.float32)
    if classification:
        def y(frame): return torch.tensor([class_to_index[label] for label in frame.label], dtype=torch.long)
    else:
        target = train.label.astype(float)
        meta.update(target_mean=float(target.mean()), target_scale=float(target.std(ddof=0)) or 1.0)
        def y(frame): return torch.tensor((frame.label.astype(float).to_numpy() - meta["target_mean"]) / meta["target_scale"], dtype=torch.float32).reshape(-1, 1)
    train_data, valid_data = TensorDataset(x(train), y(train)), TensorDataset(x(valid), y(valid))

model = build_model(TASK, meta["features"], len(classes) if classification else 1).to(device)
loss_fn = nn.CrossEntropyLoss() if classification else nn.MSELoss()
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
train_loader = DataLoader(train_data, batch_size=32, shuffle=True, num_workers=0)
valid_loader = DataLoader(valid_data, batch_size=64, num_workers=0)
curves = []
for epoch in range(1, EPOCHS + 1):
    model.train(); total_loss = 0.0
    for features, targets in train_loader:
        features, targets = features.to(device), targets.to(device)
        optimizer.zero_grad(); loss = loss_fn(model(features), targets)
        loss.backward(); optimizer.step(); total_loss += loss.item() * len(targets)
    model.eval(); validation_loss = 0.0; correct = 0
    with torch.no_grad():
        for features, targets in valid_loader:
            features, targets = features.to(device), targets.to(device)
            output = model(features); validation_loss += loss_fn(output, targets).item() * len(targets)
            if classification: correct += (output.argmax(1) == targets).sum().item()
    point = {"epoch": epoch, "train_loss": total_loss / len(train_data), "val_loss": validation_loss / len(valid_data), "learning_rate": LEARNING_RATE}
    if classification: point["val_accuracy"] = correct / len(valid_data)
    curves.append(point); print(point, flush=True)
    Path("metrics.json").write_text(json.dumps(curves), encoding="utf-8")
# Save weights, not a pickled model object. Loading still happens inside a sandbox.
torch.save(model.cpu().state_dict(), "model.pt")
Path("preprocess.json").write_text(json.dumps(meta), encoding="utf-8")
print("训练完成。模型与训练代码将由平台归档。")
'''

PREDICT_CODE = '''import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from model import build_model

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--model-dir", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--device", choices=["cpu"], default="cpu")
args = parser.parse_args()
root = Path(args.model_dir)
meta = json.loads((root / "preprocess.json").read_text(encoding="utf-8"))
frame = pd.read_csv(args.input, dtype={"sample_id": str})
classification = meta["task"] != "tabular_regression"
model = build_model(meta["task"], meta["features"], len(meta["classes"]) if classification else 1)
model.load_state_dict(torch.load(root / "model.pt", map_location="cpu", weights_only=True))
model.eval(); torch.set_num_threads(1)
predictions = []
with torch.no_grad():
    if meta["task"] == "image_classification":
        from PIL import Image
        for relative in frame.image_path:
            with Image.open(Path(args.input).parent / relative) as image:
                values = np.asarray(image.convert("L").resize((32, 32)), dtype=np.float32).copy() / 255
            output = model(torch.from_numpy(values).reshape(1, 1, 32, 32))
            predictions.append(meta["classes"][int(output.argmax(1).item())])
    else:
        values = pd.get_dummies(frame.drop(columns=["sample_id"]), dtype=float).reindex(columns=meta["columns"], fill_value=0).fillna(0).to_numpy()
        values = (values - np.array(meta["mean"])) / np.array(meta["scale"])
        output = model(torch.tensor(values, dtype=torch.float32))
        if classification: predictions = [meta["classes"][int(i)] for i in output.argmax(1)]
        else: predictions = (output.flatten().numpy() * meta["target_scale"] + meta["target_mean"]).tolist()
pd.DataFrame({"sample_id": frame.sample_id, "prediction": predictions}).to_csv(args.output, index=False)
'''


def _criterion(ident, name, description, points):
    return {'id': ident, 'name': name, 'description': description, 'source': 'ai', 'max_score': points,
        'levels': [
            {'id': 'complete', 'description': '完整满足本项公开判据，代码或分析包含直接证据', 'points': points},
            {'id': 'partial', 'description': '部分满足，但有明确遗漏或不成立的步骤，应指出具体证据', 'points': points/2},
            {'id': 'missing', 'description': '未提供相关实现或分析，或与公开要求明显不符', 'points': 0}]}


def starter_template(task_type):
    regression = task_type == 'tabular_regression'
    config = LabAssessmentConfig(task_type=task_type, metric='rmse' if regression else 'macro_f1',
        zero_baseline=1 if regression else 0, full_target=0 if regression else .9).model_dump()
    if task_type == 'notebook':
        config['criteria'] = [_criterion('implementation','实现方法','代码完成教师指定任务并处理要求的边界情况',60),
                              _criterion('analysis','实验分析','结合运行结果说明方法、局限与结论',40)]
        cells=[{'id':'instructions','type':'markdown','source':'# 实验任务\n完成教师指定任务，保留可从头执行的代码，并分析结果。','student_editable':False},
               {'id':'implementation','type':'code','source':'# 在此完成实验代码\nprint("实验开始")','student_editable':True},
               {'id':'report','type':'markdown','source':'## 实验分析\n请说明采用的方法、观察到的结果与可能的改进。','student_editable':True}]
    else:
        config['criteria'] = [
            {'id':'performance','name':'模型效果','description':'按平台独立测试集实测主指标和发布阈值折算','source':'metric','max_score':40,'levels':[]},
            _criterion('method','实现与训练方法','仅在训练集拟合预处理；模型、损失和训练循环与任务一致；推理使用相同预处理',40),
            _criterion('analysis','实验分析','比较至少两种参数配置，结合验证结果解释差异、过拟合或局限',20)]
        cells=[
            {'id':'instructions','type':'markdown','student_editable':False,
             'source':'# 模型训练实验\n读取公开训练和验证数据，运行基线；修改网络或训练参数并比较结果。正式测试由平台独立执行。模型包需包含 model.py、predict.py、model.pt、preprocess.json。'},
            {'id':'parameters','type':'code','student_editable':True,
             'source':f'TASK = {task_type!r}\nEPOCHS = 10\nLEARNING_RATE = 0.001\nSEED = 42'},
            {'id':'model','type':'code','student_editable':True,'source':'%%writefile model.py\n'+MODEL_CODE},
            {'id':'training','type':'code','student_editable':True,'source':TRAIN_CODE},
            {'id':'prediction','type':'code','student_editable':True,'source':'%%writefile predict.py\n'+PREDICT_CODE},
            {'id':'report','type':'markdown','student_editable':True,
             'source':'## 实验分析\n1. 数据包含什么，如何避免数据泄漏？\n2. 比较至少两次实验的参数与验证结果。\n3. 是否出现过拟合？依据是什么？\n4. 分析失败案例与下一步改进。'}]
    for index,cell in enumerate(cells):
        cell.update(order=index,source_hidden=False)
    config['requirements'] = '完成实验实现，比较至少两种参数设置，并依据验证结果分析方法和局限。'
    return {'cells':cells,'config':config,
        'description':'可运行教学基线。教师应调整任务与量表、补充完整参考分析并试评后发布。模型模板需要含 torch、numpy、pandas、Pillow 的环境。'}
