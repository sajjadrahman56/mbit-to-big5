# Longformer-Based Multiple Regression for Big Five with Chunking
# Author: Your Name
# Date: 2025-06-22

import os
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split
from transformers import LongformerTokenizer, LongformerModel
from sklearn.metrics import r2_score, mean_squared_error, mean_absolute_error
import json
import logging
from tqdm import tqdm
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================
# Configuration
# ============================
class Config:
    DATASET_PATH = '/kaggle/input/mbti-to-big5-numeric-values/mbti_with_big5.csv'
    MODEL_NAME = 'allenai/longformer-base-4096'
    MAX_LENGTH = 3072
    BATCH_SIZE = 8
    NUM_EPOCHS = 5
    LEARNING_RATE = 5e-5
    WEIGHT_DECAY = 1e-5
    TRAIN_VAL_SPLIT = 0.8
    TEST_PERCENTAGE = 1.0
    DROPOUT_RATE = 0.1
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    NUM_WORKERS = 0
    PIN_MEMORY = True
    MODEL_SAVE_PATH = 'longformer_big5_chunked.pth'
    RESULTS_SAVE_PATH = 'chunked_results.json'
    TRAIT_COLUMNS = ['O', 'C', 'E', 'A', 'N']
    TRAIT_NAMES = ['Openness', 'Conscientiousness', 'Extraversion', 'Agreeableness', 'Neuroticism']

# ============================
# Dataset with Chunking
# ============================
class TextRegressionDataset(Dataset):
    def __init__(self, df, tokenizer, max_length):
        self.df = df.reset_index(drop=True)
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        text = str(row['posts'])
        text = ' '.join(text.split())
        labels = torch.tensor(row[Config.TRAIT_COLUMNS].values, dtype=torch.float32)

        # Full encoding
        tokens = self.tokenizer.encode(text, add_special_tokens=True)
        chunks = [tokens[i:i + self.max_length] for i in range(0, len(tokens), self.max_length)]
        max_chunks = 3
        chunks = chunks[:max_chunks]

        input_ids = []
        attention_masks = []

        for chunk in chunks:
            padded = self.tokenizer.pad(
                {'input_ids': [chunk]},
                padding='max_length',
                max_length=self.max_length,
                return_attention_mask=True,
                return_tensors='pt'
            )
            input_ids.append(padded['input_ids'])
            attention_masks.append(padded['attention_mask'])

        input_ids = torch.cat(input_ids, dim=0)
        attention_mask = torch.cat(attention_masks, dim=0)

        return {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'labels': labels
        }

# ============================
# Model
# ============================
class LongformerForRegression(nn.Module):
    def __init__(self, model_name, num_labels=5, dropout_rate=0.1):
        super().__init__()
        self.backbone = LongformerModel.from_pretrained(model_name)
        self.dropout = nn.Dropout(dropout_rate)
        self.regressor = nn.Linear(self.backbone.config.hidden_size, num_labels)

    def forward(self, input_ids, attention_mask):
        if input_ids.ndim == 3:
            b, n, l = input_ids.shape
            input_ids = input_ids.view(-1, l)
            attention_mask = attention_mask.view(-1, l)

            outputs = self.backbone(input_ids=input_ids, attention_mask=attention_mask)
            cls_outputs = outputs.last_hidden_state[:, 0, :]
            cls_outputs = cls_outputs.view(b, n, -1)
            pooled_output = cls_outputs.mean(dim=1)
        else:
            outputs = self.backbone(input_ids=input_ids, attention_mask=attention_mask)
            pooled_output = outputs.last_hidden_state[:, 0, :]

        pooled_output = self.dropout(pooled_output)
        return self.regressor(pooled_output)

# ============================
# Trainer
# ============================
class Trainer:
    def __init__(self, model, train_loader, val_loader, device):
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.device = device
        self.criterion = nn.MSELoss()
        self.optimizer = torch.optim.AdamW(model.parameters(), lr=Config.LEARNING_RATE, weight_decay=Config.WEIGHT_DECAY)
        self.history = {'train_loss': [], 'val_loss': [], 'val_r2': []}

    def train_epoch(self):
        self.model.train()
        total_loss = 0.0
        for batch in tqdm(self.train_loader, desc='Training'):
            input_ids = batch['input_ids'].to(self.device)
            attention_mask = batch['attention_mask'].to(self.device)
            labels = batch['labels'].to(self.device)

            self.optimizer.zero_grad()
            outputs = self.model(input_ids, attention_mask)
            loss = self.criterion(outputs, labels)
            loss.backward()
            self.optimizer.step()
            total_loss += loss.item()
        return total_loss / len(self.train_loader)

    def validate(self):
        self.model.eval()
        total_loss = 0.0
        preds, targets = [], []
        with torch.no_grad():
            for batch in tqdm(self.val_loader, desc='Validating'):
                input_ids = batch['input_ids'].to(self.device)
                attention_mask = batch['attention_mask'].to(self.device)
                labels = batch['labels'].to(self.device)
                outputs = self.model(input_ids, attention_mask)
                loss = self.criterion(outputs, labels)
                total_loss += loss.item()
                preds.append(outputs.cpu().numpy())
                targets.append(labels.cpu().numpy())

        preds = np.vstack(preds)
        targets = np.vstack(targets)
        r2s = [r2_score(targets[:, i], preds[:, i]) for i in range(5)]
        return total_loss / len(self.val_loader), r2s

    def train(self, epochs):
        best_loss = float('inf')
        for epoch in range(epochs):
            logger.info(f"Epoch {epoch+1}/{epochs}")
            train_loss = self.train_epoch()
            val_loss, r2s = self.validate()
            self.history['train_loss'].append(train_loss)
            self.history['val_loss'].append(val_loss)
            self.history['val_r2'].append(r2s)

            logger.info(f"Train Loss: {train_loss:.4f}, Val Loss: {val_loss:.4f}")
            logger.info(f"R²: {[round(r, 3) for r in r2s]}")

            if val_loss < best_loss:
                best_loss = val_loss
                torch.save(self.model.state_dict(), Config.MODEL_SAVE_PATH)
        return self.history

# ============================
# Main
# ============================
def main():
    print("\n🧠 LONGFORMER CHUNKED REGRESSION MODEL")
    print("Started at:", datetime.now())

    df = pd.read_csv(Config.DATASET_PATH)
    df = df.dropna(subset=Config.TRAIT_COLUMNS)
    if Config.TEST_PERCENTAGE < 1.0:
        df = df.sample(frac=Config.TEST_PERCENTAGE, random_state=42).reset_index(drop=True)

    tokenizer = LongformerTokenizer.from_pretrained(Config.MODEL_NAME)
    dataset = TextRegressionDataset(df, tokenizer, Config.MAX_LENGTH)
    train_size = int(Config.TRAIN_VAL_SPLIT * len(dataset))
    val_size = len(dataset) - train_size
    train_ds, val_ds = random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_ds, batch_size=Config.BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=Config.BATCH_SIZE)

    model = LongformerForRegression(Config.MODEL_NAME, num_labels=5, dropout_rate=Config.DROPOUT_RATE).to(Config.DEVICE)
    trainer = Trainer(model, train_loader, val_loader, Config.DEVICE)
    history = trainer.train(Config.NUM_EPOCHS)

    with open(Config.RESULTS_SAVE_PATH, 'w') as f:
        json.dump(history, f, indent=2)

    print("\n✅ Training complete. Model saved.")

if __name__ == "__main__":
    main()
