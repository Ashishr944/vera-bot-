# 🧠 Vera Message Engine — magicpin AI Challenge

## 🚀 Overview

This project implements a **deterministic message composition engine** for **Vera**, magicpin’s AI assistant for merchant growth.

Vera helps merchants:

* Improve listings
* Run targeted campaigns
* Re-engage customers
* Respond intelligently to conversations

---

## 🎯 Objective

Build a function:

```
compose(category, merchant, trigger, customer=None)
```

Which returns:

* 📩 Next message
* 🎯 CTA (Call To Action)
* 👤 Send-as identity
* 🔁 Suppression key
* 🧠 Rationale

All outputs must be:

* Deterministic
* Context-aware
* High-engagement
* Non-generic

---

## 🧩 Key Features

### ✅ Deterministic Engine

Same input → same output (no randomness)

### ✅ Context-Aware Decisions

Uses:

* Category (tone & constraints)
* Merchant (performance, offers)
* Trigger (why message now)
* Customer (optional personalization)

### ✅ High-Compulsion Messaging

* Uses **real numbers**
* Includes **urgency**
* Focuses on **one strong action**

### ✅ Judge-Compatible API

Fully implements required endpoints:

* `/v1/healthz`
* `/v1/metadata`
* `/v1/context`
* `/v1/tick`
* `/v1/reply`

### ✅ Smart Behaviors

* Auto-reply detection
* Intent transitions
* Hostile message handling
* Suppression logic

---

## 🏗️ System Architecture

```
Incoming Context → Store → Trigger Event → compose() → Response
```

### Flow:

1. Context is pushed via `/v1/context`
2. Stored by scope (category, merchant, etc.)
3. Trigger arrives via `/v1/tick`
4. `compose()` selects best signal
5. Generates message + CTA
6. Returns structured response

---

## 🧠 Decision Strategy

### 1. Signal Prioritization

Only ONE dominant signal is used:

* Demand spike
* Performance drop
* Customer recall
* Seasonal trigger

---

### 2. Message Construction

Messages follow:

```
[Real Signal] + [Specific Data] + [Urgency] + [Action]
```

Example:

```
"190 people nearby searched for 'Dental Checkup' in last 2 hrs. Launch ₹299 offer today?"
```

---

### 3. Category Adaptation

| Category    | Tone         |
| ----------- | ------------ |
| Dentists    | Clinical     |
| Salons      | Visual       |
| Restaurants | Tempting     |
| Gyms        | Motivational |
| Pharmacies  | Utility      |

---

### 4. CTA Strategy

Each message includes:

* ONE clear action
* Low friction
* High intent

Examples:

* "Yes, launch now"
* "Fix this today"
* "Bring them back"

---

### 5. Rationale

Explains WHY message was chosen:

```
High search demand + merchant inactivity → strong conversion opportunity
```

---

## 📦 Project Structure

```
magicpin-ai-challenge/
│
├── vera_bot.py              # Main FastAPI bot
├── judge_simulator.py       # Official judge
├── requirements.txt
├── Procfile
├── runtime.txt
├── README.md
├── .env.local.example
│
├── dataset/
├── examples/
└── expanded/
```

---

## ⚙️ Setup Instructions

### 🔹 1. Clone Project

```
git clone <https://github.com/Ashishr944/vera-bot->
cd magicpin-ai-challenge
```

---

### 🔹 2. Install Dependencies

```
pip install -r requirements.txt
```

---

### 🔹 3. Run Bot Locally

```
python3 vera_bot.py --port 8080
```

---

### 🔹 4. Verify Bot

```
curl http://localhost:8080/v1/healthz
```

Expected:

```
{"status": "ok"}
```

---

## 🔐 Environment Setup

### Create `.env.local`

```
export BOT_URL="http://localhost:8080"
export LLM_PROVIDER="openrouter"
export LLM_MODEL="mistralai/mistral-7b-instruct"
export LLM_API_KEY="YOUR_API_KEY"
```

---

### Load Environment

```
source .env.local
```

---

## 🧪 Run Judge Simulator

```
python3 judge_simulator.py
```

---

## 📊 Evaluation Criteria

Each output is scored (0–10):

| Metric           | Description           |
| ---------------- | --------------------- |
| Decision Quality | Best signal selection |
| Specificity      | Real data usage       |
| Category Fit     | Tone alignment        |
| Merchant Fit     | Personalization       |
| Engagement       | Likelihood to reply   |

---

## 🌐 Deployment (Render)

### 1. Push to GitHub

```
git add .
git commit -m "final"
git push
```

---

### 2. Deploy on Render

* Create Web Service
* Connect repo

**Build Command:**

```
pip install -r requirements.txt
```

**Start Command:**

```
uvicorn vera_bot:app --host 0.0.0.0 --port $PORT
```

---

### 3. Add Environment Variables

* BOT_URL
* LLM_PROVIDER
* LLM_MODEL
* LLM_API_KEY

---

### 4. Get Public URL

```
https://your-app.onrender.com
```

---

## 🧪 Test Live Bot

```
https://your-app.onrender.com/v1/healthz
```

---

## 🧠 Design Philosophy

### 🔹 Deterministic > Creative

Consistency is more important than randomness

### 🔹 One Strong Idea

Avoid multiple weak suggestions

### 🔹 Specific > Generic

Numbers and facts outperform vague messaging

### 🔹 Action > Information

Every message must drive action

---

## ⚠️ Constraints

* No fake claims
* One CTA per message
* Must remain deterministic
* Respect session rules

---

## 📈 Example Output

```json
{
  "message": "190 people nearby searched for 'Dental Checkup'. Launch ₹299 offer today?",
  "cta": "Yes, launch now",
  "send_as": "assistant",
  "suppression_key": "demand_spike_dental",
  "rationale": "High search demand + inactive merchant"
}
```

---

## 👨‍💻 Author

Ashu Rokade

---

## 🏁 Submission Checklist

* ✅ Bot runs locally
* ✅ Judge passes all tests
* ✅ Public URL deployed
* ✅ README included
* ✅ Deterministic outputs

---
