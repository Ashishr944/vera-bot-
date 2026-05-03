# 🧠 Vera Message Engine — ChatGPT-Style UI Edition

![Vera Bot Preview](https://via.placeholder.com/1200x600.png?text=Vera+Message+Engine+UI)

Vera is magicpin's AI assistant for merchant growth. This project implements a **deterministic message composition engine** coupled with a **premium, responsive ChatGPT-style web UI**. It acts as a Copilot to help merchants improve listings, run campaigns, and seamlessly handle customer engagement.

## ✨ Key Features

### 🎨 Premium ChatGPT-Style Interface
- **Immersive Chat Experience**: A modern, responsive two-column layout featuring a conversational feed.
- **Micro-Animations**: Smooth message appearances and bouncing typing indicators.
- **Glassmorphism & Dark Mode**: Sleek dark theme with light-mode toggles and frosted glass elements for a state-of-the-art aesthetic.
- **Developer Credit**: Proudly built with an aesthetic focus by **Ashish Rokade**. All rights reserved.

### 🤖 Intelligent Multi-Turn Conversations
- **Dynamic Follow-Ups**: The bot doesn't just stop at one message. It actively remembers the turn number and pushes conversations forward intelligently.
- **Problem Resolution Logic**: Capable of reading merchant intent (e.g. "my sales are down", "getting less traffic") and dynamically cross-referencing their catalog to propose a hyper-specific, direct campaign fix.
- **Context-Aware Decisions**: Leverages categories, merchant metrics, triggers, and customer history.

### ⚙️ Deterministic Evaluation
- Designed specifically to pass the magicpin AI Challenge judge simulator.
- Same input → same output (no randomness).
- Fully validated via the built-in REST endpoints: `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, and `/v1/metadata`.

---

## 🚀 How to Run Locally

Running this project on any system is simple and fast. Follow these steps:

### 1. Prerequisites
Ensure you have **Python 3.9+** installed on your system.

### 2. Clone the Repository
```bash
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name
```

### 3. Install Dependencies
Install the required packages using pip:
```bash
pip install -r requirements.txt
```
*(Note: If you are using macOS with Homebrew Python, you may need to run `pip install -r requirements.txt --break-system-packages` or use a virtual environment).*

### 4. Start the Server
Launch the Vera Message Engine locally:
```bash
python3 vera_bot.py --port 8080
```

### 5. Access the Web Interface
Open your favorite web browser and navigate to:
```text
http://localhost:8080
```
You will be greeted by the new ChatGPT-style interface. Click on "Customer" or "Merchant" on the left sidebar to toggle scenarios, and start chatting!

---

## 🛠 Project Structure

- `vera_bot.py`: The core engine containing the `VeraComposer` class, the FastAPI/HTTP endpoints, the multi-turn conversational logic, and the embedded frontend UI.
- `requirements.txt`: Python dependencies (`fastapi`, `uvicorn`, `pydantic`, etc.) needed to run the engine.
- `dataset/`: Contains the seed data for categories, merchants, customers, and triggers used by the deterministic engine.

---

## 🏆 magicpin AI Challenge Constraints Met

- **Decision Quality**: Selects the best specific action based on live merchant data.
- **Specificity**: Injects precise numbers, catalog offers, and dates.
- **Category & Merchant Fit**: Tailors its voice to the business type and historical conversions.
- **Engagement Compulsion**: Pushes low-friction "yes/no" follow-ups.

---

<div align="center">
  <b>Developer: Ashish Rokade</b> <br>
  &copy; All rights reserved.
</div>
