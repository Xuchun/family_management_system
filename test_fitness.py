import sqlite3
import pandas as pd
import sys
import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()
DB_FILE = 'tasks.db'
key = os.getenv("DB_ENCRYPTION_KEY")
if key:
    cipher_suite = Fernet(key.strip("'\" ").encode())
else:
    cipher_suite = None

def decrypt_str(enc_text):
    if not cipher_suite or not enc_text: return enc_text
    try:
        if isinstance(enc_text, (int, float)): enc_text = str(enc_text)
        return cipher_suite.decrypt(enc_text.encode()).decode()
    except: return enc_text

def get_latest_fitness_records():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_fitness_records ORDER BY record_date DESC, id DESC", conn)
            latest_records = {}
            if not df.empty:
                for _, row in df.iterrows():
                    ex = decrypt_str(row['exercise'])
                    if ex not in latest_records:
                        latest_records[ex] = {
                            'weight': float(decrypt_str(row['weight'])),
                            'reps': int(decrypt_str(row['reps'])),
                            'sets': int(decrypt_str(row['sets']))
                        }
            return latest_records
    except Exception as e:
        return str(e)

print(get_latest_fitness_records())
