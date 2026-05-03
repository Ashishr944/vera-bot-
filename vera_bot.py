#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


UTC = timezone.utc
AUTO_REPLY_PATTERNS = [
    "thank you for contacting us",
    "our team will respond shortly",
    "we will get back to you",
    "auto reply",
    "automated message",
    "away right now",
]
HOSTILE_PATTERNS = [
    "stop messaging",
    "spam",
    "useless",
    "dont message",
    "don't message",
    "remove me",
    "unsubscribe",
    "stop",
]
LATER_PATTERNS = ["later", "tomorrow", "busy", "after", "not now"]
POSITIVE_PATTERNS = [
    "yes",
    "ok",
    "okay",
    "lets do it",
    "let's do it",
    "what next",
    "sounds good",
    "send it",
    "do it",
    "proceed",
    "go ahead",
]
NEGATIVE_PATTERNS = ["no", "not interested", "don't", "do not", "skip", "nah"]


@dataclass
class ComposedMessage:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]
    follow_up_body: Optional[str] = None
    next_step_label: Optional[str] = None


@dataclass
class StoredContext:
    version: int
    payload: dict[str, Any]
    delivered_at: str


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: str
    scope: str
    composed: ComposedMessage
    created_at: str
    last_turn: int = 1
    last_message_at: Optional[str] = None
    auto_reply_count: int = 0
    ended: bool = False


@dataclass
class DemoConversation:
    conversation_id: str
    scenario_id: str
    category: dict[str, Any]
    merchant: dict[str, Any]
    trigger: dict[str, Any]
    customer: Optional[dict[str, Any]]
    composed: ComposedMessage
    scope: str
    created_at: str
    last_turn: int = 1
    auto_reply_count: int = 0
    ended: bool = False


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    normalized = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def title_case_words(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().title()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def format_pct(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{abs(value) * 100:.0f}%"


def ordinal_hint(label: str) -> str:
    return label.replace("_", " ").replace("-", " ")


def contains_any(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in patterns)


def merchant_first_name(merchant: dict[str, Any]) -> str:
    owner = merchant.get("identity", {}).get("owner_first_name")
    if owner:
        return str(owner).replace("Dr. ", "").split()[0]
    name = merchant.get("identity", {}).get("name", "there")
    return str(name).split()[0]


def merchant_display_name(merchant: dict[str, Any], category: dict[str, Any]) -> str:
    first = merchant_first_name(merchant)
    if category.get("slug") == "dentists":
        return f"Dr. {first}"
    return first


def choose_active_offer(merchant: dict[str, Any], keywords: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    active = [offer for offer in merchant.get("offers", []) if offer.get("status") == "active"]
    if not active:
        return None
    if keywords:
        for offer in active:
            title = offer.get("title", "").lower()
            if any(keyword.lower() in title for keyword in keywords):
                return offer
    return active[0]


def choose_catalog_offer(category: dict[str, Any], keywords: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    offers = category.get("offer_catalog", [])
    if keywords:
        for offer in offers:
            title = offer.get("title", "").lower()
            if any(keyword.lower() in title for keyword in keywords):
                return offer
    return offers[0] if offers else None


def find_digest_item(category: dict[str, Any], item_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not item_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


def recent_merchant_reply(merchant: dict[str, Any]) -> Optional[dict[str, Any]]:
    history = merchant.get("conversation_history", [])
    merchant_msgs = [item for item in history if item.get("from") == "merchant"]
    return merchant_msgs[-1] if merchant_msgs else None


def recent_vera_touch(merchant: dict[str, Any]) -> Optional[dict[str, Any]]:
    history = merchant.get("conversation_history", [])
    vera_msgs = [item for item in history if item.get("from") == "vera"]
    return vera_msgs[-1] if vera_msgs else None


def build_conversation_id(trigger: dict[str, Any]) -> str:
    seed = f"{trigger.get('merchant_id')}|{trigger.get('customer_id')}|{trigger.get('id')}|{trigger.get('suppression_key')}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"conv_{digest}"


def build_template_name(scope: str, category_slug: str, trigger_kind: str) -> str:
    return f"{scope}_{category_slug}_{trigger_kind}_v1"


def month_from_trigger(trigger: dict[str, Any], now: Optional[datetime]) -> Optional[int]:
    for key in ("match_time_iso", "date", "due_date", "stock_runs_out_iso"):
        value = trigger.get("payload", {}).get(key)
        parsed = parse_iso(value) if "T" in str(value) else None
        if parsed:
            return parsed.month
    return now.month if now else None


class VeraComposer:
    def compose(
        self,
        category: dict[str, Any],
        merchant: dict[str, Any],
        trigger: dict[str, Any],
        customer: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> ComposedMessage:
        scope = trigger.get("scope", "merchant")
        if scope == "customer" and customer:
            return self._compose_customer(category, merchant, trigger, customer, now)
        return self._compose_merchant(category, merchant, trigger, now)

    def _compose_merchant(
        self,
        category: dict[str, Any],
        merchant: dict[str, Any],
        trigger: dict[str, Any],
        now: Optional[datetime],
    ) -> ComposedMessage:
        kind = trigger.get("kind", "")
        handler = getattr(self, f"_merchant_{kind}", None)
        if handler:
            return handler(category, merchant, trigger, now)
        return self._merchant_generic(category, merchant, trigger, now)

    def _compose_customer(
        self,
        category: dict[str, Any],
        merchant: dict[str, Any],
        trigger: dict[str, Any],
        customer: dict[str, Any],
        now: Optional[datetime],
    ) -> ComposedMessage:
        kind = trigger.get("kind", "")
        handler = getattr(self, f"_customer_{kind}", None)
        if handler:
            return handler(category, merchant, trigger, customer, now)
        return self._customer_generic(category, merchant, trigger, customer, now)

    def _finalize(
        self,
        body: str,
        cta: str,
        send_as: str,
        trigger: dict[str, Any],
        category: dict[str, Any],
        rationale: str,
        follow_up_body: Optional[str] = None,
        next_step_label: Optional[str] = None,
    ) -> ComposedMessage:
        body = compact_spaces(body)
        params = [segment.strip() for segment in re.split(r"[?.!]\s*", body) if segment.strip()][:3]
        return ComposedMessage(
            body=body,
            cta=cta,
            send_as=send_as,
            suppression_key=trigger.get("suppression_key", trigger.get("id", "")),
            rationale=rationale,
            template_name=build_template_name(trigger.get("scope", "merchant"), category.get("slug", "generic"), trigger.get("kind", "message")),
            template_params=params,
            follow_up_body=follow_up_body,
            next_step_label=next_step_label,
        )

    def _merchant_generic(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        metric = payload.get("metric_or_topic") or payload.get("metric") or trigger.get("kind", "update")
        offer = choose_active_offer(merchant) or choose_catalog_offer(category)
        offer_bit = f" around {offer.get('title')}" if offer else ""
        body = (
            f"{greeting}, quick heads-up on {ordinal_hint(str(metric))}. "
            f"I can turn this into one practical message{offer_bit} so you can act on it today. "
            f"Want me to draft it?"
        )
        rationale = "Fallback composer used the trigger topic plus the merchant's live offer to keep the ask actionable."
        follow_up = f"Done. I'll draft a short operator-style message around {offer.get('title')} and keep it ready for review." if offer else "Done. I'll draft a short, practical message and keep it ready for review."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "draft")

    def _merchant_research_digest(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        item = find_digest_item(category, trigger.get("payload", {}).get("top_item_id")) or category.get("digest", [None])[0]
        greeting = merchant_display_name(merchant, category)
        cohort = merchant.get("customer_aggregate", {}).get("high_risk_adult_count")
        cohort_bit = f"your {cohort} high-risk adults" if cohort else "your case-mix"
        trial_n = item.get("trial_n")
        result_snippet = item.get("summary", "").split(".")[0] if item else "A relevant category update landed."
        evidence = f"{trial_n:,}-patient trial" if trial_n else item.get("title", "new digest item")
        body = (
            f"{greeting}, {item.get('source', 'this week')} has one item worth a look for {cohort_bit} — "
            f"{evidence} says {result_snippet.lower()}. "
            f"Want me to pull the 2-min takeaway + draft one patient-facing WhatsApp from it?"
        )
        follow_up = (
            f"Done. Key takeaway: {item.get('actionable', result_snippet)}. "
            f"I can also draft a patient WhatsApp using your existing voice if you want to post it next."
        )
        rationale = "Used the cited digest item and the merchant's high-risk cohort so the message feels clinically relevant instead of generic."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "digest_takeaway")

    def _merchant_regulation_change(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        item = find_digest_item(category, trigger.get("payload", {}).get("top_item_id"))
        greeting = merchant_display_name(merchant, category)
        deadline = trigger.get("payload", {}).get("deadline_iso", "")
        deadline_short = deadline[:10] if deadline else "the stated deadline"
        body = (
            f"{greeting}, compliance heads-up: {item.get('title', 'a rule changed')} is effective by {deadline_short}. "
            f"{item.get('summary', '').split('.')[0]}. "
            f"Want a 3-point checklist you can hand to staff today?"
        )
        follow_up = f"Here’s the checklist: 1) {item.get('actionable', 'review the change')} 2) note the deadline {deadline_short} 3) update your SOP before the next audit cycle."
        rationale = "Lead with the deadline and one concrete compliance action because urgency matters more than promotional copy here."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "checklist")

    def _merchant_cde_opportunity(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        item = find_digest_item(category, trigger.get("payload", {}).get("digest_item_id"))
        greeting = merchant_display_name(merchant, category)
        date_value = item.get("date", "")
        when = date_value[:16].replace("T", " ") if date_value else "soon"
        credits = trigger.get("payload", {}).get("credits", item.get("credits"))
        body = (
            f"{greeting}, small CPD/CDE opportunity: {item.get('title', 'a relevant session')} is on {when}. "
            f"{credits} credits, source: {item.get('source', 'category calendar')}. "
            f"Want the one-screen summary so you can decide in 30 seconds?"
        )
        follow_up = f"Summary: {item.get('summary', 'Relevant upskilling session')}. Action note: {item.get('actionable', 'evaluate whether it fits your practice mix')}."
        rationale = "Framed the webinar as a fast decision with source, credits, and timing so it feels useful rather than noisy."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "summary")

    def _merchant_competitor_opened(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        offer = choose_active_offer(merchant) or choose_catalog_offer(category)
        offer_bit = offer.get("title") if offer else "your strongest existing offer"
        body = (
            f"{greeting}, new competitor alert: {payload.get('competitor_name', 'a nearby clinic')} opened {payload.get('distance_km', '?')} km away with "
            f"{payload.get('their_offer', 'an entry offer')}. "
            f"Don’t race to the bottom. Better move: sharpen visibility around {offer_bit}. Want me to draft the counter-positioning copy?"
        )
        follow_up = f"Draft angle: 'verified local practice, clear pricing, and {offer_bit}' so you compete on trust plus clarity, not just a lower sticker price."
        rationale = "Turned the competitor signal into a differentiated response tied to the merchant's own offer instead of recommending a blind discount."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "counter_copy")

    def _merchant_perf_dip(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        metric = payload.get("metric", "performance")
        delta = format_pct(payload.get("delta_pct"))
        baseline = payload.get("vs_baseline")
        offer = choose_active_offer(merchant) or choose_catalog_offer(category)
        offer_status = "You have no live offer right now."
        if choose_active_offer(merchant) and offer:
            offer_status = f"You already have {offer.get('title')} live."
        body = (
            f"{greeting}, your {metric} is down {delta} over the last {payload.get('window', '7d')} "
            f"(baseline {baseline if baseline is not None else 'noted'}). "
            f"{offer_status} "
            f"Want me to draft the single best fix for this week?"
        )
        fix = (
            f"Best immediate fix: push {offer.get('title')} with one locality-specific post and one short WhatsApp reply template."
            if offer else
            "Best immediate fix: launch one specific service+price offer from your category catalog and push it in GBP plus WhatsApp."
        )
        rationale = "Used the exact underperforming metric and chose one concrete corrective action to avoid overwhelming the merchant."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, fix, "fix_plan")

    def _merchant_perf_spike(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        driver = payload.get("likely_driver")
        driver_note = f"Likely driver: {driver}." if driver else ""
        body = (
            f"{greeting}, nice signal: {payload.get('metric', 'performance')} is up {format_pct(payload.get('delta_pct'))} in the last {payload.get('window', '7d')}. "
            f"{driver_note} "
            f"Want me to turn this into a repeatable post so you keep the momentum?"
        )
        follow_up = f"I’d repeat the winning angle once more this week, then convert it into a saved reply so the extra demand becomes easier to close."
        rationale = "Acknowledged the positive signal and converted it into a repeatable operating action instead of a vague congratulations note."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "repeat_play")

    def _merchant_milestone_reached(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        body = (
            f"{greeting}, you're at {payload.get('value_now')} reviews and {payload.get('milestone_value')} is within reach. "
            f"This is the right moment to ask only your happiest recent customers. Want a 2-line review request you can send today?"
        )
        follow_up = f"Draft: 'Thanks for visiting {merchant.get('identity', {}).get('name')}. If the visit felt smooth, a quick Google review today would really help local discovery.'"
        rationale = "Used the near-term milestone to create urgency and suggested a lightweight review ask tied to timing."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "review_request")

    def _merchant_active_planning_intent(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        topic = trigger.get("payload", {}).get("intent_topic", "")
        greeting = merchant_display_name(merchant, category)
        if topic == "corporate_bulk_thali_package":
            body = (
                f"{greeting}, here’s a clean starter pack for your corporate thali idea in {merchant.get('identity', {}).get('locality', 'your area')}: "
                f"10 @ ₹125, 25 @ ₹115, 50+ @ ₹105, with day-before confirmation by 5pm. "
                f"Want me to turn this into a WhatsApp pitch for office admins?"
            )
            follow_up = (
                f"Draft pitch: '{merchant.get('identity', {}).get('name')} corporate thali for nearby offices — fresh lunch, predictable delivery window, and volume pricing from 10 meals upward.'"
            )
            rationale = "The merchant already asked 'what would it look like', so the best next step is a concrete starter artifact rather than more qualifying questions."
            return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "pitch")
        if topic == "kids_yoga_summer_camp":
            body = (
                f"{greeting}, starter shape for the kids program: 4 weeks, 3 classes/week, age 7-12, Saturday parent showcase at the end. "
                f"Your next move is packaging, not brainstorming. Want me to draft the GBP post + parent WhatsApp copy?"
            )
            follow_up = "Done. I’ll package it as a confidence + flexibility camp, keep the copy parent-friendly, and make the CTA one-line simple."
            rationale = "The merchant is already in action mode, so the response delivers a draftable program structure instead of asking for more input."
            return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "program_copy")
        return self._merchant_generic(category, merchant, trigger, now)

    def _merchant_seasonal_perf_dip(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        active_members = merchant.get("customer_aggregate", {}).get("total_active_members")
        body = (
            f"{greeting}, your {payload.get('metric', 'views')} is down {format_pct(payload.get('delta_pct'))} this week, "
            f"but this looks like the normal {payload.get('season_note', 'seasonal lull').replace('_', ' ')} window. "
            f"Skip panic spend; protect your {active_members if active_members is not None else 'current'} members instead. "
            f"Want a retention challenge you can launch this week?"
        )
        follow_up = "Retention play: 21-day attendance streak, simple progress check-ins, and one public leaderboard slot so members have a reason to come back this week."
        rationale = "Reframed the dip using seasonal context and redirected the merchant toward retention, which is the smarter decision in this window."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "retention_challenge")

    def _merchant_festival_upcoming(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        offer = choose_active_offer(merchant) or choose_catalog_offer(category)
        payload = trigger.get("payload", {})
        offer_note = f"You already have {offer.get('title')};" if offer else "A strong category-fit offer exists;"
        body = (
            f"{greeting}, {payload.get('festival', 'the coming festival')} is {payload.get('days_until', '?')} days away. "
            f"Best move is one clear festive offer, not a generic discount blast. "
            f"{offer_note} want me to turn it into festival copy now?"
        )
        follow_up = f"I’d frame it around {offer.get('title') if offer else 'one category-fit offer'} with a short deadline and one reply CTA so responses stay easy."
        rationale = "Seasonal timing plus an existing offer creates a stronger, more believable campaign than inventing a new discount."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "festival_copy")

    def _merchant_ipl_match_today(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        offer = choose_active_offer(merchant, ["pizza", "combo", "delivery"]) or choose_active_offer(merchant) or choose_catalog_offer(category, ["combo", "pizza"])
        weekend = not payload.get("is_weeknight", True)
        delta_note = "-12% restaurant covers" if weekend else "+18% covers"
        angle = "delivery-first tonight" if weekend else "match-night dine-in push tonight"
        body = (
            f"Quick heads-up {greeting} — {payload.get('match')} at {payload.get('venue')} starts {payload.get('match_time_iso', '')[11:16]}. "
            f"{'Weekend IPL usually means ' + delta_note if weekend else 'Weeknight IPL has been ' + delta_note} this season. "
            f"Best move: {angle} using {offer.get('title') if offer else 'your live offer'}. Want me to draft the banner + story copy?"
        )
        follow_up = f"Draft angle: '{offer.get('title') if offer else 'Match-night special'} for home-watch orders tonight' with one strong CTA and no extra conditions."
        rationale = "The message uses the match context plus the merchant's live offer to recommend the most likely-to-work promotion format for tonight."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "match_copy")

    def _merchant_review_theme_emerged(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        quote = payload.get("common_quote")
        quote_note = f'Example: "{quote}". ' if quote else ""
        body = (
            f"{greeting}, {payload.get('occurrences_30d')} recent reviews are clustering around '{payload.get('theme')}'. "
            f"{quote_note}"
            f"This is worth fixing before pushing more traffic. Want a 2-step response plan?"
        )
        follow_up = "Plan: first, address the operating cause this week. Second, add one public reply theme so future readers see the issue is being handled."
        rationale = "Focused on the strongest negative review theme because fixing conversion blockers usually beats sending more top-of-funnel traffic."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "response_plan")

    def _merchant_curious_ask_due(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        body = (
            f"Hi {greeting}! Quick check — what service has been most asked for this week at {merchant.get('identity', {}).get('name')}? "
            f"I’ll turn your answer into one Google post + one saved WhatsApp reply. Takes 2 minutes."
        )
        follow_up = "Perfect. Send me the top service name only and I’ll convert it into ready-to-use copy."
        rationale = "Curious asks work best when the merchant only needs to reply with one short fact and gets a concrete deliverable back."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "prompt")

    def _merchant_supply_alert(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        batches = ", ".join(payload.get("affected_batches", []))
        chronic = merchant.get("customer_aggregate", {}).get("chronic_rx_customer_count") or merchant.get("customer_aggregate", {}).get("total_chronic_rx") or merchant.get("customer_aggregate", {}).get("total_unique_ytd")
        body = (
            f"{greeting}, urgent stock alert: voluntary recall on {payload.get('molecule')} batches {batches} from {payload.get('manufacturer')}. "
            f"This matters because you have repeat-Rx volume{f' (~{chronic})' if chronic else ''}. "
            f"Want a pull-list + customer WhatsApp draft right now?"
        )
        follow_up = f"Immediate steps: isolate batches {batches}, log distributor return, then send a calm replacement message to affected repeat customers."
        rationale = "Compliance and patient trust are the highest-priority levers here, so the message asks to act immediately on specific recalled batches."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "pull_list")

    def _merchant_category_seasonal(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        trends = trigger.get("payload", {}).get("trends", [])
        first_two = ", ".join(trends[:2])
        body = (
            f"{greeting}, seasonal demand shift is already visible: {first_two}. "
            f"This is usually a shelf-mix decision before it becomes a sales decision. Want a quick action list for front counter vs back shelf?"
        )
        follow_up = "Front counter: fastest-growing essentials. Back shelf: slowing categories. That keeps the customer decision easy at the point of sale."
        rationale = "Converted seasonal demand changes into a practical merchandising decision because that is the fastest merchant action."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "action_list")

    def _merchant_renewal_due(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        views = merchant.get("performance", {}).get("views")
        body = (
            f"{greeting}, your {payload.get('plan')} plan renews in {payload.get('days_remaining')} days. "
            f"Before that, I’d rather help you use the remaining window well — you're sitting on {views} recent views but only {merchant.get('performance', {}).get('calls')} calls. "
            f"Want the fastest improvement play before renewal hits?"
        )
        follow_up = "Fastest play: tighten one live offer, refresh your latest post, and fix the top conversion blocker before the renewal date."
        rationale = "Anchored the renewal reminder to live performance so the conversation still feels growth-oriented, not just billing-oriented."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "improvement_play")

    def _merchant_winback_eligible(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        body = (
            f"{greeting}, you’ve been expired for {payload.get('days_since_expiry')} days and performance dipped {format_pct(payload.get('perf_dip_pct'))} since then. "
            f"That’s long enough to measure impact. Want a low-friction winback plan focused on the {payload.get('lapsed_customers_added_since_expiry')} customers you've lost since expiry?"
        )
        follow_up = "Winback angle: restart with one hero offer, reactivate lapsed regulars first, then fix profile visibility so paid visibility has something to convert."
        rationale = "Used the post-expiry decline and lapsed-customer count to make the winback case concrete and financially grounded."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "winback_plan")

    def _merchant_dormant_with_vera(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        body = (
            f"{greeting}, we’ve been quiet for {payload.get('days_since_last_merchant_message')} days. "
            f"Instead of another generic reminder: what’s the one thing blocking growth right now — calls, walk-ins, or repeat customers? "
            f"Reply with one word and I’ll keep the next step specific."
        )
        follow_up = "Perfect. Send just the blocker and I’ll respond with one practical next move, not a long checklist."
        rationale = "For a dormant merchant, a diagnostic one-word reply is lower friction than pitching a full campaign out of nowhere."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "diagnostic")

    def _merchant_gbp_unverified(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        greeting = merchant_display_name(merchant, category)
        payload = trigger.get("payload", {})
        uplift = format_pct(payload.get("estimated_uplift_pct"))
        body = (
            f"{greeting}, your GBP is still unverified. That usually blocks trust and can cost roughly {uplift} visibility in this category. "
            f"Verification is one high-leverage task, not a marketing nice-to-have. Want the shortest route — phone call vs postcard?"
        )
        follow_up = f"Shortest path: {payload.get('verification_path', 'use the fastest available verification path')}. Once verified, we can tighten the profile copy and offers."
        rationale = "Verification is a foundational bottleneck, so the message emphasizes the concrete downside and offers a simple path decision."
        return self._finalize(body, "open_ended", "vera", trigger, category, rationale, follow_up, "verification_path")

    def _customer_generic(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there").split(" (")[0]
        owner = merchant_first_name(merchant)
        body = (
            f"Hi {customer_name}, {owner} from {merchant.get('identity', {}).get('name')} here. "
            f"Quick follow-up from your recent history with us. Want me to help with the next step?"
        )
        rationale = "Fallback customer outreach keeps the note brief, identity-linked, and reply-friendly when trigger detail is sparse."
        follow_up = "Thanks. Tell me what works for you and we’ll keep it simple from here."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "assist")

    def _customer_recall_due(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there").split(" (")[0]
        payload = trigger.get("payload", {})
        slots = payload.get("available_slots", [])
        offer = choose_active_offer(merchant, ["cleaning"]) or choose_active_offer(merchant)
        slot_text = ""
        if len(slots) >= 2:
            slot_text = f"{slots[0].get('label')} or {slots[1].get('label')}"
        elif slots:
            slot_text = slots[0].get("label")
        slot_note = f"We can hold {slot_text}. " if slot_text else ""
        offer_note = f"{offer.get('title')} is live right now. " if offer else ""
        body = (
            f"Hi {customer_name}, {merchant.get('identity', {}).get('name')} here. "
            f"Your {payload.get('service_due', 'recall').replace('_', ' ')} is due. "
            f"{slot_note}"
            f"{offer_note}"
            f"Reply 1 for the first slot, 2 for the second, or send a better time."
        )
        follow_up = "Done. We’ll hold your preferred slot and confirm once you reply with the option number or time."
        rationale = "Customer recall copy uses due timing, real slots, and the merchant's active offer to make the next action easy."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "slot_hold")

    def _customer_wedding_package_followup(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there")
        owner = merchant_first_name(merchant)
        payload = trigger.get("payload", {})
        preferred = customer.get("preferences", {}).get("preferred_slots", "Saturday")
        body = (
            f"Hi {customer_name}, {owner} from {merchant.get('identity', {}).get('name')} here. "
            f"{payload.get('days_to_wedding')} days to your wedding means the skin-prep window is open now, especially since you already did your bridal trial. "
            f"Want me to hold a {preferred.replace('_', ' ')} consult slot for the next step?"
        )
        follow_up = "Lovely. I’ll block a consult-style slot first so the plan can match your wedding timeline instead of guessing."
        rationale = "Used the wedding timeline and prior bridal trial so the follow-up feels timely and relationship-aware."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "consult_hold")

    def _customer_customer_lapsed_hard(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there")
        owner = merchant_first_name(merchant)
        focus = trigger.get("payload", {}).get("previous_focus") or customer.get("preferences", {}).get("training_focus", "your training")
        offer = choose_active_offer(merchant, ["trial", "month"]) or choose_active_offer(merchant)
        body = (
            f"Hi {customer_name}, {owner} from {merchant.get('identity', {}).get('name')} here. "
            f"It’s been about {trigger.get('payload', {}).get('days_since_last_visit')} days — happens to most members, no stress. "
            f"If {focus.replace('_', ' ')} is still the goal, {offer.get('title') if offer else 'we can restart gently this week'} makes a low-pressure return easy. "
            f"Want me to hold a comeback slot for you?"
        )
        follow_up = "Done. We’ll keep the restart low-pressure and build from one easy session instead of pushing a full commitment."
        rationale = "Winback message removes shame, recalls the customer's goal, and offers a low-friction re-entry path."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "comeback_slot")

    def _customer_customer_lapsed_soft(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there")
        offer = choose_active_offer(merchant) or choose_catalog_offer(category)
        offer_note = f"{offer.get('title')} is available if that helps. " if offer else ""
        body = (
            f"Hi {customer_name}, quick nudge from {merchant.get('identity', {}).get('name')}. "
            f"You’ve gone a little quiet with us, so this is just a gentle check-in. "
            f"{offer_note}"
            f"Want me to suggest the easiest next visit option?"
        )
        follow_up = "Sure. I’ll suggest the simplest restart option based on your past visits and timing preference."
        rationale = "Soft-lapse copy stays warm and low-pressure while still giving one concrete reason to reply."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "restart_option")

    def _customer_trial_followup(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there").split(" (")[0]
        payload = trigger.get("payload", {})
        options = payload.get("next_session_options", [])
        option_text = options[0].get("label") if options else "the next session"
        body = (
            f"Hi {customer_name}, thanks again for trying {merchant.get('identity', {}).get('name')}. "
            f"The next good slot is {option_text}. Want me to hold it for you?"
        )
        follow_up = "Done. We’ll hold that next session once you confirm."
        rationale = "Trial follow-up keeps the ask binary and grounded in the next actual session option."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "trial_hold")

    def _customer_chronic_refill_due(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there")
        payload = trigger.get("payload", {})
        meds = ", ".join(payload.get("molecule_list", [])[:3])
        due = payload.get("stock_runs_out_iso", "")[:10]
        saved_address = payload.get("delivery_address_saved")
        body = (
            f"Namaste {customer_name}, refill reminder from {merchant.get('identity', {}).get('name')}. "
            f"Your {meds} supply may run out around {due}. "
            f"{'Saved address is on file for delivery. ' if saved_address else ''}"
            f"Reply YES and we’ll prepare the refill check."
        )
        follow_up = "Received. We’ll get the refill check prepared and keep delivery simple."
        rationale = "Refill reminders work best when they mention the actual molecules, estimated stock-out date, and whether delivery is already easy."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "refill_check")

    def _customer_appointment_tomorrow(self, category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any], now: Optional[datetime]) -> ComposedMessage:
        customer_name = customer.get("identity", {}).get("name", "there")
        body = (
            f"Hi {customer_name}, reminder from {merchant.get('identity', {}).get('name')} for your appointment tomorrow. "
            f"Reply YES to confirm or send another time if needed."
        )
        follow_up = "Thanks. We’ll note the confirmation or help with a reschedule."
        rationale = "Appointment reminders should be clean, logistical, and easy to confirm."
        return self._finalize(body, "open_ended", "merchant_on_behalf", trigger, category, rationale, follow_up, "confirm")


def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: Optional[dict[str, Any]] = None,
) -> ComposedMessage:
    return VeraComposer().compose(category, merchant, trigger, customer)


def render_homepage() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vera • Merchant Growth Copilot</title>
  <style>
    :root {
      --bg: #0c111d;
      --panel: rgba(16, 24, 40, 0.82);
      --panel-strong: rgba(12, 18, 31, 0.94);
      --line: rgba(148, 163, 184, 0.18);
      --text: #edf2f7;
      --muted: #9fb0c8;
      --accent: #f97316;
      --accent-soft: rgba(249, 115, 22, 0.14);
      --accent-2: #22c55e;
      --clinical: #38bdf8;
      --care: #f472b6;
      --energy: #f59e0b;
      --focus: #34d399;
      --shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
      --radius: 26px;
    }
    body[data-theme="light"] {
      --bg: #f4f7fb;
      --panel: rgba(255, 255, 255, 0.82);
      --panel-strong: rgba(255, 255, 255, 0.96);
      --line: rgba(71, 85, 105, 0.14);
      --text: #0f172a;
      --muted: #52637a;
      --accent-soft: rgba(249, 115, 22, 0.10);
      --shadow: 0 20px 60px rgba(15, 23, 42, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "Satoshi", "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.18), transparent 28%),
        radial-gradient(circle at top right, rgba(249, 115, 22, 0.20), transparent 30%),
        linear-gradient(160deg, #08101b 0%, #0f172a 45%, #111827 100%);
    }
    body[data-theme="light"] {
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.10), transparent 28%),
        radial-gradient(circle at top right, rgba(249, 115, 22, 0.14), transparent 30%),
        linear-gradient(160deg, #f8fafc 0%, #eff6ff 45%, #eef2ff 100%);
    }
    .shell {
      width: min(1440px, calc(100vw - 32px));
      margin: 16px auto;
      border: 1px solid var(--line);
      border-radius: 34px;
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
      box-shadow: var(--shadow);
      overflow: hidden;
      backdrop-filter: blur(20px);
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: rgba(7, 13, 24, 0.58);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand-mark {
      width: 44px;
      height: 44px;
      border-radius: 14px;
      background: linear-gradient(135deg, #fb923c, #f97316 48%, #facc15);
      display: grid;
      place-items: center;
      color: white;
      font-weight: 700;
      letter-spacing: 0.06em;
      box-shadow: 0 10px 28px rgba(249, 115, 22, 0.35);
    }
    .brand-copy h1 {
      margin: 0;
      font-size: 1rem;
      line-height: 1.1;
      letter-spacing: 0.02em;
    }
    .brand-copy p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 0.87rem;
    }
    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }
    .pill {
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--muted);
      font-size: 0.8rem;
      white-space: nowrap;
    }
    .theme-toggle {
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-size: 0.8rem;
    }
    .layout {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr) 310px;
      min-height: calc(100vh - 120px);
    }
    .chat-stage {
      padding: 22px;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at 50% 0%, rgba(251, 146, 60, 0.10), transparent 30%),
        linear-gradient(180deg, rgba(3,7,18,0.4), rgba(3,7,18,0.04));
      border-bottom: 1px solid var(--line);
    }
    .phone {
      width: min(430px, 100%);
      border-radius: 34px;
      border: 1px solid rgba(255,255,255,0.12);
      background: linear-gradient(180deg, rgba(2,6,23,0.94), rgba(15,23,42,0.94));
      box-shadow: 0 28px 80px rgba(0,0,0,0.38);
      overflow: hidden;
      position: relative;
      animation: phoneLift 620ms ease both;
      transform-origin: center bottom;
    }
    .phone::before {
      content: "";
      position: absolute;
      top: 10px;
      left: 50%;
      transform: translateX(-50%);
      width: 120px;
      height: 24px;
      border-radius: 999px;
      background: rgba(0,0,0,0.42);
      z-index: 3;
    }
    .phone-head {
      padding: 42px 18px 14px;
      background: linear-gradient(180deg, rgba(249,115,22,0.22), rgba(255,255,255,0.02));
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .phone-profile {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .avatar {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, #fb923c, #f97316);
      color: white;
      font-weight: 700;
      box-shadow: 0 10px 28px rgba(249, 115, 22, 0.35);
    }
    .phone-profile strong {
      display: block;
      font-size: 0.92rem;
      margin-bottom: 2px;
    }
    .phone-profile span {
      color: var(--muted);
      font-size: 0.78rem;
    }
    .phone-badge {
      font-size: 0.74rem;
      color: #d9e4f2;
      padding: 8px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.05);
    }
    .sidebar, .insights {
      padding: 24px;
      border-right: 1px solid var(--line);
      background: rgba(6, 11, 21, 0.44);
    }
    .insights {
      border-right: 0;
      border-left: 1px solid var(--line);
    }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 16px;
    }
    .section-title h2, .chat-title h2 {
      margin: 0;
      font-size: 0.98rem;
      letter-spacing: 0.01em;
    }
    .section-title span, .chat-title span {
      color: var(--muted);
      font-size: 0.78rem;
    }
    .hero {
      padding: 18px;
      border-radius: 24px;
      border: 1px solid rgba(249, 115, 22, 0.24);
      background: linear-gradient(180deg, rgba(249,115,22,0.14), rgba(255,255,255,0.03));
      margin-bottom: 20px;
      animation: fadeUp 520ms ease both;
      animation-delay: 80ms;
    }
    .hero h3 {
      margin: 0 0 10px;
      font-size: 1.5rem;
      line-height: 1.05;
    }
    .hero p {
      margin: 0;
      color: #dbe7f5;
      font-size: 0.92rem;
      line-height: 1.6;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .metric {
      padding: 12px;
      border-radius: 18px;
      background: rgba(6, 11, 21, 0.44);
      border: 1px solid var(--line);
    }
    .metric b { display: block; font-size: 1.15rem; margin-bottom: 4px; }
    .metric small { color: var(--muted); }
    .scenario-list {
      display: grid;
      gap: 12px;
    }
    .scenario {
      width: 100%;
      text-align: left;
      padding: 16px;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      color: var(--text);
      cursor: pointer;
      transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
      animation: fadeUp 440ms ease both;
    }
    .scenario:hover, .scenario.active {
      transform: translateY(-4px) scale(1.01);
      border-color: rgba(255,255,255,0.22);
      background: rgba(255,255,255,0.06);
      box-shadow: 0 18px 40px rgba(0,0,0,0.18);
    }
    .scenario .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.75rem;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .scenario-head {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }
    .scenario-avatar {
      width: 38px;
      height: 38px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, rgba(251,146,60,0.24), rgba(56,189,248,0.24));
      border: 1px solid var(--line);
      font-size: 0.85rem;
      font-weight: 700;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
    }
    .accent-clinical { background: var(--clinical); }
    .accent-care { background: var(--care); }
    .accent-energy { background: var(--energy); }
    .accent-focus { background: var(--focus); }
    .scenario strong {
      display: block;
      font-size: 1rem;
      margin-bottom: 8px;
    }
    .scenario p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 0.86rem;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
      overflow: hidden;
    }
    .chat-wrap {
      display: grid;
      grid-template-rows: auto 1fr auto;
      background:
        radial-gradient(circle at top, rgba(56, 189, 248, 0.08), transparent 32%),
        linear-gradient(180deg, rgba(3,7,18,0.65), rgba(3,7,18,0.88));
    }
    .chat-head {
      padding: 24px 24px 18px;
      border-bottom: 1px solid var(--line);
    }
    .chat-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 10px;
    }
    .headline {
      display: grid;
      gap: 8px;
    }
    .headline h3 {
      margin: 0;
      font-size: 1.45rem;
    }
    .headline p {
      margin: 0;
      color: var(--muted);
      font-size: 0.93rem;
      line-height: 1.5;
      max-width: 700px;
    }
    .stack {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .tag {
      padding: 10px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.8rem;
    }
    .messages {
      padding: 24px;
      display: grid;
      gap: 16px;
      align-content: start;
      overflow: auto;
      min-height: 420px;
      max-height: calc(100vh - 330px);
    }
    .message {
      max-width: 82%;
      padding: 16px 18px;
      border-radius: 24px;
      line-height: 1.65;
      font-size: 0.96rem;
      position: relative;
      box-shadow: 0 12px 24px rgba(0,0,0,0.18);
      animation: rise 220ms ease;
    }
    .message small {
      display: block;
      margin-top: 10px;
      color: rgba(255,255,255,0.64);
      font-size: 0.75rem;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .bot {
      background: linear-gradient(180deg, rgba(249,115,22,0.22), rgba(255,255,255,0.04));
      border: 1px solid rgba(249,115,22,0.24);
      justify-self: start;
    }
    .user {
      background: rgba(56, 189, 248, 0.14);
      border: 1px solid rgba(56, 189, 248, 0.22);
      justify-self: end;
    }
    .composer {
      padding: 18px 24px 24px;
      border-top: 1px solid var(--line);
      background: rgba(7, 11, 21, 0.92);
    }
    .typing {
      justify-self: start;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 14px 16px;
      border-radius: 20px;
      border: 1px solid rgba(249,115,22,0.24);
      background: rgba(249,115,22,0.12);
      box-shadow: 0 12px 24px rgba(0,0,0,0.18);
    }
    .typing span {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #f7c9a3;
      animation: blink 1s infinite ease-in-out;
    }
    .typing span:nth-child(2) { animation-delay: 120ms; }
    .typing span:nth-child(3) { animation-delay: 240ms; }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .chip {
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 999px;
      cursor: pointer;
      font-size: 0.84rem;
    }
    .compose-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
    }
    textarea {
      width: 100%;
      resize: none;
      min-height: 58px;
      max-height: 140px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      padding: 16px 18px;
      font: inherit;
      outline: none;
    }
    textarea::placeholder { color: #7f91a9; }
    .send {
      border: 0;
      border-radius: 18px;
      padding: 0 18px;
      min-width: 124px;
      font: inherit;
      font-weight: 700;
      color: white;
      background: linear-gradient(135deg, #fb923c, #f97316 60%, #ea580c);
      cursor: pointer;
      box-shadow: 0 14px 28px rgba(249, 115, 22, 0.28);
    }
    .insight-card {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--line);
      margin-bottom: 14px;
    }
    .insight-card h4 {
      margin: 0 0 10px;
      font-size: 0.95rem;
    }
    .insight-card p, .insight-card li {
      margin: 0;
      color: var(--muted);
      font-size: 0.87rem;
      line-height: 1.55;
    }
    .insight-card ul {
      padding-left: 18px;
      margin: 0;
    }
    .context-grid {
      display: grid;
      gap: 14px;
      margin-top: 12px;
    }
    .context-box {
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
    }
    .context-box h5 {
      margin: 0 0 8px;
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
    }
    .context-box pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.78rem;
      line-height: 1.55;
      color: var(--text);
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
    }
    .rail {
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      overflow: hidden;
      margin-top: 10px;
    }
    .rail > span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #38bdf8, #f97316);
    }
    .footer-note {
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.5;
      margin-top: 14px;
    }
    .drawer {
      margin-top: 18px;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      overflow: hidden;
    }
    .drawer-head {
      width: 100%;
      border: 0;
      background: rgba(255,255,255,0.02);
      color: var(--text);
      padding: 14px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font: inherit;
      cursor: pointer;
    }
    .drawer pre {
      margin: 0;
      padding: 16px;
      color: #d7e3f4;
      font-size: 0.78rem;
      line-height: 1.6;
      overflow: auto;
      max-height: 360px;
      background: rgba(2,6,23,0.42);
    }
    .drawer-copy {
      padding: 0 16px 16px;
    }
    .copy-btn {
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 14px;
      cursor: pointer;
      font: inherit;
      font-size: 0.82rem;
    }
    .view-toggle {
      display: inline-flex;
      gap: 8px;
      padding: 6px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      margin-top: 14px;
    }
    .view-toggle button {
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 10px 14px;
      border-radius: 999px;
      cursor: pointer;
      font: inherit;
      font-size: 0.82rem;
    }
    .view-toggle button.active {
      background: rgba(249,115,22,0.18);
      color: var(--text);
    }
    .hidden-by-view {
      display: none !important;
    }
    @keyframes rise {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(18px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes phoneLift {
      from { opacity: 0; transform: translateY(30px) scale(0.96); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes blink {
      0%, 80%, 100% { transform: scale(0.7); opacity: 0.45; }
      40% { transform: scale(1); opacity: 1; }
    }
    @media (max-width: 1180px) {
      .layout { grid-template-columns: 300px 1fr; }
      .insights { display: none; }
    }
    @media (max-width: 860px) {
      .shell { width: calc(100vw - 16px); margin: 8px auto; border-radius: 26px; }
      .layout { grid-template-columns: 1fr; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--line); }
      .messages { max-height: none; min-height: 320px; }
      .compose-row { grid-template-columns: 1fr; }
      .send { min-height: 54px; }
      .message { max-width: 100%; }
      .topbar { flex-direction: column; align-items: flex-start; }
      .status-row { justify-content: flex-start; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <div class="brand-mark">V</div>
        <div class="brand-copy">
          <h1>Vera Message Engine</h1>
          <p>Deterministic merchant growth copilot for listings, campaigns, and reply flows.</p>
        </div>
      </div>
      <div class="status-row">
        <div class="pill">Deterministic compose()</div>
        <div class="pill">Judge-ready API surface</div>
        <div class="pill">Modern demo chat UI</div>
        <button class="theme-toggle" id="themeToggle" type="button">Light Mode</button>
      </div>
    </div>
    <div class="layout">
      <aside class="sidebar">
        <div class="hero">
          <h3>Sharper outreach, grounded in merchant context.</h3>
          <p>Preview how Vera picks the right message, tone, CTA, and rationale from category, merchant, trigger, and customer context.</p>
          <div class="metric-grid">
            <div class="metric"><b>5</b><small>verticals</small></div>
            <div class="metric"><b>25+</b><small>seed triggers</small></div>
            <div class="metric"><b>Stateful</b><small>reply routing</small></div>
            <div class="metric"><b>Fast</b><small>HTTP demo</small></div>
          </div>
        </div>
        <div class="section-title">
          <h2>Demo Scenarios</h2>
          <span>Tap to load</span>
        </div>
        <div class="scenario-list" id="scenarioList"></div>
      </aside>
      <main class="chat-wrap">
        <div class="chat-head">
          <div class="chat-title">
            <h2>Live Preview</h2>
            <span id="engineLabel">Vera</span>
          </div>
          <div class="headline">
            <h3 id="scenarioTitle">Choose a scenario</h3>
            <p id="scenarioSubtitle">The message preview will appear here with the same deterministic logic used by the API endpoints.</p>
          </div>
          <div class="stack" id="metaTags"></div>
          <div class="view-toggle">
            <button id="merchantViewBtn" class="active" type="button">Merchant View</button>
            <button id="customerViewBtn" type="button">Customer View</button>
          </div>
        </div>
        <div class="chat-stage">
          <div class="phone">
            <div class="phone-head">
              <div class="phone-profile">
                <div class="avatar">V</div>
                <div>
                  <strong>Vera</strong>
                  <span>merchant growth assistant</span>
                </div>
              </div>
              <div class="phone-badge" id="phoneBadge">live demo</div>
            </div>
            <div class="messages" id="messages">
              <div class="message bot">
                Pick a scenario from the left to watch Vera compose the next best message.
                <small>vera</small>
              </div>
            </div>
          </div>
        </div>
        <div class="composer">
          <div class="chips" id="chips"></div>
          <div class="compose-row">
            <textarea id="replyBox" placeholder="Type a merchant or customer reply to continue the flow..."></textarea>
            <button class="send" id="sendBtn">Send Reply</button>
          </div>
          <div class="drawer">
            <button class="drawer-head" id="drawerToggle" type="button">
              <span>Deterministic Payload</span>
              <span id="drawerState">show</span>
            </button>
            <div id="drawerBody" hidden>
              <pre id="rawPayload">{}</pre>
              <div class="drawer-copy">
                <button class="copy-btn" id="copyPayload" type="button">Copy JSON</button>
              </div>
            </div>
          </div>
        </div>
      </main>
      <aside class="insights">
        <div class="section-title">
          <h2>Why It Works</h2>
          <span>Scoring lens</span>
        </div>
        <div class="insight-card">
          <h4>Decision Quality</h4>
          <p>Vera ranks active triggers first, then chooses the single signal most worth messaging on now.</p>
          <div class="rail"><span style="width:84%"></span></div>
        </div>
        <div class="insight-card">
          <h4>Specificity</h4>
          <p>Messages lean on real prices, source names, metric deltas, appointment slots, and category-safe detail.</p>
          <div class="rail"><span style="width:90%"></span></div>
        </div>
        <div class="insight-card">
          <h4>Merchant Fit</h4>
          <ul>
            <li>Uses live offers when present</li>
            <li>Adapts to city, locality, and behavior</li>
            <li>Shifts voice by vertical</li>
          </ul>
        </div>
        <div class="insight-card">
          <h4>Replay Safety</h4>
          <ul>
            <li>Detects auto-replies</li>
            <li>Ends hostile threads immediately</li>
            <li>Moves to action after positive intent</li>
          </ul>
        </div>
        <div class="insight-card">
          <h4>Judge Mode</h4>
          <p>Live structured context for the active scenario.</p>
          <div class="context-grid">
            <div class="context-box">
              <h5>Category</h5>
              <pre id="judgeCategory">Choose a scenario</pre>
            </div>
            <div class="context-box">
              <h5>Merchant</h5>
              <pre id="judgeMerchant">Choose a scenario</pre>
            </div>
            <div class="context-box">
              <h5>Trigger</h5>
              <pre id="judgeTrigger">Choose a scenario</pre>
            </div>
            <div class="context-box">
              <h5>Customer</h5>
              <pre id="judgeCustomer">Choose a scenario</pre>
            </div>
          </div>
        </div>
        <p class="footer-note">Judge-facing endpoints still live under <code>/v1/*</code>. This page is a product-style preview layered on top of the same deterministic engine.</p>
      </aside>
    </div>
  </div>
  <script>
    const state = { scenarios: [], activeScenario: null, conversationId: null, view: "merchant" };

    const els = {
      scenarioList: document.getElementById("scenarioList"),
      messages: document.getElementById("messages"),
      scenarioTitle: document.getElementById("scenarioTitle"),
      scenarioSubtitle: document.getElementById("scenarioSubtitle"),
      metaTags: document.getElementById("metaTags"),
      chips: document.getElementById("chips"),
      replyBox: document.getElementById("replyBox"),
      sendBtn: document.getElementById("sendBtn"),
      engineLabel: document.getElementById("engineLabel"),
      phoneBadge: document.getElementById("phoneBadge"),
      rawPayload: document.getElementById("rawPayload"),
      drawerBody: document.getElementById("drawerBody"),
      drawerState: document.getElementById("drawerState"),
      drawerToggle: document.getElementById("drawerToggle"),
      copyPayload: document.getElementById("copyPayload"),
      judgeCategory: document.getElementById("judgeCategory"),
      judgeMerchant: document.getElementById("judgeMerchant"),
      judgeTrigger: document.getElementById("judgeTrigger"),
      judgeCustomer: document.getElementById("judgeCustomer"),
      themeToggle: document.getElementById("themeToggle"),
      merchantViewBtn: document.getElementById("merchantViewBtn"),
      customerViewBtn: document.getElementById("customerViewBtn"),
    };

    function accentClass(accent) {
      return `accent-${accent || "clinical"}`;
    }

    function addMessage(role, body) {
      const div = document.createElement("div");
      div.className = `message ${role === "user" ? "user" : "bot"}`;
      const small = role === "user" ? "merchant" : "vera";
      div.innerHTML = `${body}<small>${small}</small>`;
      els.messages.appendChild(div);
      els.messages.scrollTop = els.messages.scrollHeight;
    }

    function setRawPayload(payload) {
      els.rawPayload.textContent = JSON.stringify(payload || {}, null, 2);
    }

    function setJudgeContext(rawContext) {
      els.judgeCategory.textContent = JSON.stringify(rawContext?.category || {}, null, 2);
      els.judgeMerchant.textContent = JSON.stringify(rawContext?.merchant || {}, null, 2);
      els.judgeTrigger.textContent = JSON.stringify(rawContext?.trigger || {}, null, 2);
      els.judgeCustomer.textContent = JSON.stringify(rawContext?.customer ?? null, null, 2);
    }

    function applyViewFilter() {
      const wantCustomer = state.view === "customer";
      document.querySelectorAll(".scenario").forEach((node, index) => {
        const scenario = state.scenarios[index];
        const shouldHide = wantCustomer ? scenario.scope !== "customer" : scenario.scope === "customer";
        node.classList.toggle("hidden-by-view", shouldHide);
      });
      els.merchantViewBtn.classList.toggle("active", state.view === "merchant");
      els.customerViewBtn.classList.toggle("active", state.view === "customer");
    }

    function setTags(tags) {
      els.metaTags.innerHTML = "";
      tags.forEach((tag) => {
        const node = document.createElement("div");
        node.className = "tag";
        node.textContent = tag;
        els.metaTags.appendChild(node);
      });
    }

    function setChips(chips) {
      els.chips.innerHTML = "";
      chips.forEach((chip) => {
        const button = document.createElement("button");
        button.className = "chip";
        button.type = "button";
        button.textContent = chip;
        button.onclick = () => {
          els.replyBox.value = chip;
          sendReply();
        };
        els.chips.appendChild(button);
      });
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      return response.json();
    }

    function showTyping() {
      const div = document.createElement("div");
      div.className = "typing";
      div.id = "typingBubble";
      div.innerHTML = "<span></span><span></span><span></span>";
      els.messages.appendChild(div);
      els.messages.scrollTop = els.messages.scrollHeight;
    }

    function hideTyping() {
      document.getElementById("typingBubble")?.remove();
    }

    async function loadScenarios() {
      const data = await fetchJson("/demo/scenarios");
      state.scenarios = data.scenarios || [];
      els.scenarioList.innerHTML = "";
      state.scenarios.forEach((scenario, index) => {
        const button = document.createElement("button");
        button.className = "scenario";
        button.style.animationDelay = `${index * 70}ms`;
        button.innerHTML = `
          <div class="scenario-head">
            <div class="scenario-avatar" style="background:${scenario.avatar_gradient || "linear-gradient(135deg, #fb923c, #f97316)"}">${scenario.avatar || "V"}</div>
            <div>
              <div class="eyebrow"><span class="dot ${accentClass(scenario.accent)}"></span>${scenario.eyebrow}</div>
              <strong>${scenario.label}</strong>
            </div>
          </div>
          <p>${scenario.body_preview}</p>
        `;
        button.onclick = () => openScenario(scenario.id);
        els.scenarioList.appendChild(button);
      });
      applyViewFilter();
      const preferred = state.scenarios.find((scenario) => scenario.scope === (state.view === "customer" ? "customer" : "merchant")) || state.scenarios[0];
      if (preferred) openScenario(preferred.id);
    }

    async function openScenario(id) {
      const data = await fetchJson("/demo/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario_id: id })
      });
      state.activeScenario = data.scenario;
      state.conversationId = data.conversation_id;
      document.querySelectorAll(".scenario").forEach((node, index) => {
        node.classList.toggle("active", state.scenarios[index].id === id);
      });
      els.messages.innerHTML = "";
      addMessage("bot", data.message.body);
      els.scenarioTitle.textContent = `${data.scenario.label} • ${data.scenario.merchant_name}`;
      els.scenarioSubtitle.textContent = `Trigger: ${data.scenario.trigger_kind} • City: ${data.scenario.merchant_city} • Send-as: ${data.scenario.send_as}`;
      els.engineLabel.textContent = data.message.cta || "open_ended";
      els.phoneBadge.textContent = data.scenario.eyebrow.toLowerCase();
      setTags([data.scenario.eyebrow, data.scenario.trigger_kind, data.scenario.send_as, data.message.cta]);
      setChips(data.chips || []);
      setRawPayload(data.raw);
      setJudgeContext(data.raw_context);
      els.replyBox.value = "";
    }

    async function sendReply() {
      const value = els.replyBox.value.trim();
      if (!value || !state.conversationId) return;
      addMessage("user", value);
      els.replyBox.value = "";
      showTyping();
      const data = await fetchJson("/demo/reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: state.conversationId, message: value })
      });
      hideTyping();
      addMessage("bot", data.body || "No reply generated.");
      setRawPayload(data.raw);
    }

    els.sendBtn.onclick = sendReply;
    els.replyBox.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendReply();
      }
    });

    els.drawerToggle.onclick = () => {
      const hidden = els.drawerBody.hasAttribute("hidden");
      if (hidden) {
        els.drawerBody.removeAttribute("hidden");
        els.drawerState.textContent = "hide";
      } else {
        els.drawerBody.setAttribute("hidden", "");
        els.drawerState.textContent = "show";
      }
    };

    els.copyPayload.onclick = async () => {
      await navigator.clipboard.writeText(els.rawPayload.textContent);
      els.copyPayload.textContent = "Copied";
      setTimeout(() => { els.copyPayload.textContent = "Copy JSON"; }, 1200);
    };

    els.themeToggle.onclick = () => {
      const next = document.body.dataset.theme === "light" ? "dark" : "light";
      document.body.dataset.theme = next;
      els.themeToggle.textContent = next === "light" ? "Dark Mode" : "Light Mode";
    };

    els.merchantViewBtn.onclick = () => {
      state.view = "merchant";
      applyViewFilter();
      const candidate = state.scenarios.find((scenario) => scenario.scope !== "customer");
      if (candidate) openScenario(candidate.id);
    };

    els.customerViewBtn.onclick = () => {
      state.view = "customer";
      applyViewFilter();
      const candidate = state.scenarios.find((scenario) => scenario.scope === "customer");
      if (candidate) openScenario(candidate.id);
    };

    loadScenarios();
  </script>
</body>
</html>"""


class VeraBotState:
    def __init__(self, metadata: Optional[dict[str, Any]] = None) -> None:
        self.started_at = time.time()
        self.contexts: dict[str, dict[str, StoredContext]] = {
            "category": {},
            "merchant": {},
            "customer": {},
            "trigger": {},
        }
        self.sent_suppressions: set[str] = set()
        self.conversations: dict[str, ConversationState] = {}
        self.demo_conversations: dict[str, DemoConversation] = {}
        self.metadata = metadata or {
            "team_name": "Vera Deterministic Engine",
            "team_members": ["OpenAI Codex"],
            "model": "rule-based-deterministic",
            "approach": "dependency-free composer with trigger ranking, context grounding, and stateful reply routing",
            "contact_email": "team@example.com",
            "version": "0.1.0",
            "submitted_at": utc_now_iso(),
        }
        self.composer = VeraComposer()
        self.project_root = Path(__file__).resolve().parent

    def load_dataset_seeds(self, root: Path) -> None:
        categories_dir = root / "dataset" / "categories"
        if categories_dir.exists():
            for path in categories_dir.glob("*.json"):
                payload = json.loads(path.read_text())
                self.store_context("category", payload["slug"], 1, payload, utc_now_iso())

    def contexts_loaded(self) -> dict[str, int]:
        return {scope: len(items) for scope, items in self.contexts.items()}

    def _load_seed_collection(self, filename: str, key: str) -> list[dict[str, Any]]:
        payload = json.loads((self.project_root / "dataset" / filename).read_text())
        return payload.get(key, [])

    def get_demo_scenarios(self) -> list[dict[str, Any]]:
        categories = {
            item["slug"]: item for item in [json.loads(path.read_text()) for path in (self.project_root / "dataset" / "categories").glob("*.json")]
        }
        merchants = {item["merchant_id"]: item for item in self._load_seed_collection("merchants_seed.json", "merchants")}
        customers = {item["customer_id"]: item for item in self._load_seed_collection("customers_seed.json", "customers")}
        triggers = {item["id"]: item for item in self._load_seed_collection("triggers_seed.json", "triggers")}

        catalog = [
            {
                "id": "research_digest",
                "label": "Research Digest",
                "eyebrow": "Dentists",
                "merchant_id": "m_001_drmeera_dentist_delhi",
                "trigger_id": "trg_001_research_digest_dentists",
                "customer_id": None,
                "accent": "clinical",
            },
            {
                "id": "customer_recall",
                "label": "Recall Reminder",
                "eyebrow": "Customer Outreach",
                "merchant_id": "m_001_drmeera_dentist_delhi",
                "trigger_id": "trg_003_recall_due_priya",
                "customer_id": "c_001_priya_for_m001",
                "accent": "care",
            },
            {
                "id": "match_night",
                "label": "Match Night Offer",
                "eyebrow": "Restaurants",
                "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                "trigger_id": "trg_010_ipl_match_delhi",
                "customer_id": None,
                "accent": "energy",
            },
            {
                "id": "retention_reframe",
                "label": "Seasonal Dip Reframe",
                "eyebrow": "Gyms",
                "merchant_id": "m_007_powerhouse_gym_bangalore",
                "trigger_id": "trg_014_seasonal_acquisition_dip_powerhouse",
                "customer_id": None,
                "accent": "focus",
            },
        ]

        scenarios = []
        for item in catalog:
            merchant = deepcopy(merchants[item["merchant_id"]])
            trigger = deepcopy(triggers[item["trigger_id"]])
            category = deepcopy(categories[merchant["category_slug"]])
            customer = deepcopy(customers[item["customer_id"]]) if item["customer_id"] else None
            composed = self.composer.compose(category, merchant, trigger, customer)
            scenarios.append(
                {
                    "id": item["id"],
                    "label": item["label"],
                    "eyebrow": item["eyebrow"],
                    "accent": item["accent"],
                    "avatar": merchant["identity"]["name"][:1].upper(),
                    "avatar_gradient": {
                        "clinical": "linear-gradient(135deg, #38bdf8, #0ea5e9)",
                        "care": "linear-gradient(135deg, #f472b6, #fb7185)",
                        "energy": "linear-gradient(135deg, #f59e0b, #f97316)",
                        "focus": "linear-gradient(135deg, #34d399, #14b8a6)",
                    }.get(item["accent"], "linear-gradient(135deg, #fb923c, #f97316)"),
                    "merchant_name": merchant["identity"]["name"],
                    "merchant_city": merchant["identity"]["city"],
                    "trigger_kind": trigger["kind"],
                    "body_preview": composed.body,
                    "cta": composed.cta,
                    "send_as": composed.send_as,
                    "scope": trigger.get("scope", "merchant"),
                    "raw_context": {
                        "category": {
                            "slug": category.get("slug"),
                            "voice_tone": category.get("voice", {}).get("tone"),
                            "seasonal_beats": category.get("seasonal_beats", [])[:2],
                        },
                        "merchant": {
                            "name": merchant["identity"]["name"],
                            "city": merchant["identity"]["city"],
                            "locality": merchant["identity"]["locality"],
                            "signals": merchant.get("signals", [])[:4],
                            "offers": [offer.get("title") for offer in merchant.get("offers", [])[:3]],
                            "performance": merchant.get("performance", {}),
                        },
                        "trigger": trigger,
                        "customer": customer,
                    },
                }
            )
        return scenarios

    def start_demo_conversation(self, scenario_id: str) -> tuple[int, dict[str, Any]]:
        scenario_map = {item["id"]: item for item in self.get_demo_scenarios()}
        if scenario_id not in scenario_map:
            return 404, {"error": "unknown_scenario"}

        merchants = {item["merchant_id"]: item for item in self._load_seed_collection("merchants_seed.json", "merchants")}
        customers = {item["customer_id"]: item for item in self._load_seed_collection("customers_seed.json", "customers")}
        triggers = {item["id"]: item for item in self._load_seed_collection("triggers_seed.json", "triggers")}
        categories = {
            item["slug"]: item for item in [json.loads(path.read_text()) for path in (self.project_root / "dataset" / "categories").glob("*.json")]
        }
        mapping = {
            "research_digest": ("m_001_drmeera_dentist_delhi", "trg_001_research_digest_dentists", None),
            "customer_recall": ("m_001_drmeera_dentist_delhi", "trg_003_recall_due_priya", "c_001_priya_for_m001"),
            "match_night": ("m_005_pizzajunction_restaurant_delhi", "trg_010_ipl_match_delhi", None),
            "retention_reframe": ("m_007_powerhouse_gym_bangalore", "trg_014_seasonal_acquisition_dip_powerhouse", None),
        }
        merchant_id, trigger_id, customer_id = mapping[scenario_id]
        merchant = deepcopy(merchants[merchant_id])
        trigger = deepcopy(triggers[trigger_id])
        customer = deepcopy(customers[customer_id]) if customer_id else None
        category = deepcopy(categories[merchant["category_slug"]])
        composed = self.composer.compose(category, merchant, trigger, customer)
        conversation_id = f"demo_{scenario_id}"
        self.demo_conversations[conversation_id] = DemoConversation(
            conversation_id=conversation_id,
            scenario_id=scenario_id,
            category=category,
            merchant=merchant,
            trigger=trigger,
            customer=customer,
            composed=composed,
            scope=trigger.get("scope", "merchant"),
            created_at=utc_now_iso(),
        )
        return 200, {
            "conversation_id": conversation_id,
            "scenario": scenario_map[scenario_id],
            "message": {
                "role": composed.send_as,
                "body": composed.body,
                "cta": composed.cta,
                "rationale": composed.rationale,
            },
            "raw": {
                "body": composed.body,
                "cta": composed.cta,
                "send_as": composed.send_as,
                "suppression_key": composed.suppression_key,
                "rationale": composed.rationale,
                "template_name": composed.template_name,
                "template_params": composed.template_params,
            },
            "raw_context": scenario_map[scenario_id].get("raw_context"),
            "chips": self._demo_reply_chips(trigger),
        }

    def _demo_reply_chips(self, trigger: dict[str, Any]) -> list[str]:
        if trigger.get("scope") == "customer":
            return ["1", "2", "Need another time"]
        if trigger.get("kind") == "active_planning_intent":
            return ["Yes, draft it", "Make it sharper", "Not now"]
        return ["Yes, do it", "What's the exact copy?", "Not now"]

    def demo_reply(self, conversation_id: str, message: str) -> tuple[int, dict[str, Any]]:
        convo = self.demo_conversations.get(conversation_id)
        if not convo:
            return 404, {"error": "unknown_conversation"}
        message = compact_spaces(message)
        if contains_any(message, HOSTILE_PATTERNS):
            convo.ended = True
            return 200, {
                "action": "end",
                "body": "Understood. Vera would stop here and leave the thread clean.",
                "raw": {"action": "end", "rationale": "Recipient explicitly opted out."},
            }
        if contains_any(message, AUTO_REPLY_PATTERNS):
            convo.ended = True
            return 200, {
                "action": "end",
                "body": "Auto-reply detected. Vera would end the thread to avoid wasting turns.",
                "raw": {"action": "end", "rationale": "Detected auto-reply pattern."},
            }
        if contains_any(message, LATER_PATTERNS):
            return 200, {
                "action": "wait",
                "body": "Makes sense. Vera would wait 30 minutes before nudging again.",
                "raw": {"action": "wait", "wait_seconds": 1800, "rationale": "User asked for time."},
            }
        if contains_any(message, POSITIVE_PATTERNS) or re.fullmatch(r"[12]", message):
            body = self._demo_follow_up(convo, message)
            return 200, {
                "action": "send",
                "body": body,
                "raw": {"action": "send", "body": body, "cta": "open_ended", "rationale": "Positive intent detected."},
            }
        if contains_any(message, NEGATIVE_PATTERNS):
            convo.ended = True
            return 200, {
                "action": "end",
                "body": "No problem. Vera would close the loop and avoid further follow-up.",
                "raw": {"action": "end", "rationale": "Negative response received."},
            }
        body = self._demo_contextual_reply(convo, message)
        return 200, {
            "action": "send",
            "body": body,
            "raw": {"action": "send", "body": body, "cta": "open_ended", "rationale": "Contextual demo continuation."},
        }

    def _demo_follow_up(self, convo: DemoConversation, message: str) -> str:
        if convo.scope == "customer" and re.fullmatch(r"[12]", message):
            slots = convo.trigger.get("payload", {}).get("available_slots", [])
            idx = int(message) - 1
            if 0 <= idx < len(slots):
                return f"Perfect — I’ve noted {slots[idx].get('label')}. We’ll confirm that slot shortly."
        if convo.composed.follow_up_body:
            return convo.composed.follow_up_body
        return "Done. Vera would move into the next concrete step here."

    def _demo_contextual_reply(self, convo: DemoConversation, message: str) -> str:
        if convo.trigger.get("kind") == "curious_ask_due":
            return f"Useful. Vera would turn '{message}' into a Google post plus a saved reply."
        if convo.scope == "customer":
            return "Thanks. Vera would keep this easy and ask for the best time that works."
        greeting = merchant_display_name(convo.merchant, convo.category)
        return f"Thanks, {greeting}. Vera would keep the next suggestion specific to '{message}' and move straight to action."

    def store_context(self, scope: str, context_id: str, version: int, payload: dict[str, Any], delivered_at: str) -> tuple[int, dict[str, Any]]:
        if scope not in self.contexts:
            return 400, {"accepted": False, "reason": "invalid_scope", "details": scope}
        current = self.contexts[scope].get(context_id)
        if current and version <= current.version:
            return 409, {"accepted": False, "reason": "stale_version", "current_version": current.version}
        self.contexts[scope][context_id] = StoredContext(version=version, payload=deepcopy(payload), delivered_at=delivered_at)
        return 200, {"accepted": True, "ack_id": f"ack_{slugify(context_id)}_v{version}", "stored_at": utc_now_iso()}

    def get_payload(self, scope: str, context_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not context_id:
            return None
        stored = self.contexts.get(scope, {}).get(context_id)
        return deepcopy(stored.payload) if stored else None

    def active_conversation_for_target(self, merchant_id: str, customer_id: Optional[str]) -> bool:
        for convo in self.conversations.values():
            if convo.ended:
                continue
            if convo.merchant_id == merchant_id and convo.customer_id == customer_id:
                return True
        return False

    def tick(self, now_iso: str, available_triggers: list[str]) -> dict[str, Any]:
        now = parse_iso(now_iso) or datetime.now(UTC)
        ranked: dict[tuple[str, Optional[str]], tuple[int, dict[str, Any]]] = {}
        for trigger_id in available_triggers:
            trigger = self.get_payload("trigger", trigger_id)
            if not trigger:
                continue
            if trigger.get("suppression_key") in self.sent_suppressions:
                continue
            expires_at = parse_iso(trigger.get("expires_at"))
            if expires_at and expires_at < now:
                continue
            merchant_id = trigger.get("merchant_id")
            customer_id = trigger.get("customer_id")
            merchant = self.get_payload("merchant", merchant_id)
            if not merchant:
                continue
            if self.active_conversation_for_target(merchant_id, customer_id):
                continue
            category = self.get_payload("category", merchant.get("category_slug"))
            if not category:
                continue
            customer = self.get_payload("customer", customer_id) if customer_id else None
            score = self._rank_trigger(category, merchant, trigger, customer, now)
            key = (merchant_id, customer_id)
            prev = ranked.get(key)
            if prev is None or score > prev[0]:
                ranked[key] = (score, trigger)

        actions: list[dict[str, Any]] = []
        for _, trigger in sorted(ranked.values(), key=lambda pair: (-pair[0], pair[1].get("id", ""))):
            merchant = self.get_payload("merchant", trigger.get("merchant_id"))
            category = self.get_payload("category", merchant.get("category_slug")) if merchant else None
            customer = self.get_payload("customer", trigger.get("customer_id")) if trigger.get("customer_id") else None
            if not merchant or not category:
                continue
            composed = self.composer.compose(category, merchant, trigger, customer, now)
            conversation_id = build_conversation_id(trigger)
            actions.append(
                {
                    "conversation_id": conversation_id,
                    "merchant_id": trigger.get("merchant_id"),
                    "customer_id": trigger.get("customer_id"),
                    "send_as": composed.send_as,
                    "trigger_id": trigger.get("id"),
                    "template_name": composed.template_name,
                    "template_params": composed.template_params,
                    "body": composed.body,
                    "cta": composed.cta,
                    "suppression_key": composed.suppression_key,
                    "rationale": composed.rationale,
                }
            )
            self.sent_suppressions.add(composed.suppression_key)
            self.conversations[conversation_id] = ConversationState(
                conversation_id=conversation_id,
                merchant_id=trigger.get("merchant_id"),
                customer_id=trigger.get("customer_id"),
                trigger_id=trigger.get("id"),
                scope=trigger.get("scope", "merchant"),
                composed=composed,
                created_at=now_iso,
                last_message_at=now_iso,
            )
        return {"actions": actions}

    def _rank_trigger(
        self,
        category: dict[str, Any],
        merchant: dict[str, Any],
        trigger: dict[str, Any],
        customer: Optional[dict[str, Any]],
        now: datetime,
    ) -> int:
        kind = trigger.get("kind", "")
        urgency = int(trigger.get("urgency", 1))
        base = {
            "active_planning_intent": 120,
            "supply_alert": 115,
            "regulation_change": 105,
            "review_theme_emerged": 95,
            "research_digest": 90,
            "competitor_opened": 88,
            "seasonal_perf_dip": 86,
            "perf_dip": 84,
            "customer_lapsed_hard": 84,
            "recall_due": 82,
            "chronic_refill_due": 82,
            "ipl_match_today": 80,
            "trial_followup": 78,
            "festival_upcoming": 74,
            "renewal_due": 70,
            "gbp_unverified": 70,
            "milestone_reached": 68,
            "perf_spike": 64,
            "curious_ask_due": 62,
            "dormant_with_vera": 58,
        }.get(kind, 55)
        score = base + urgency * 7
        signals = merchant.get("signals", [])
        if kind == "active_planning_intent" and recent_merchant_reply(merchant):
            score += 18
        if kind in {"perf_dip", "seasonal_perf_dip"} and any("perf_dip" in signal or "ctr_below_peer" in signal for signal in signals):
            score += 10
        if kind == "research_digest" and category.get("slug") == "dentists" and merchant.get("customer_aggregate", {}).get("high_risk_adult_count"):
            score += 12
        if kind == "ipl_match_today" and choose_active_offer(merchant, ["pizza", "combo", "delivery"]):
            score += 9
        if kind == "customer_lapsed_hard" and customer and customer.get("preferences", {}).get("reminder_opt_in"):
            score += 8
        if kind == "curious_ask_due" and any("high_engagement" in signal or "engaged_in_last" in signal for signal in signals):
            score += 8
        if kind == "renewal_due" and merchant.get("subscription", {}).get("status") == "trial":
            score += 6
        if kind == "dormant_with_vera" and recent_vera_touch(merchant):
            last = parse_iso(recent_vera_touch(merchant).get("ts"))
            if last and now - last < timedelta(days=3):
                score -= 10
        if trigger.get("scope") == "customer" and customer and not customer.get("preferences", {}).get("reminder_opt_in", False):
            score -= 100
        return score

    def reply(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        message = compact_spaces(payload.get("message", ""))
        conversation_id = payload.get("conversation_id")
        merchant_id = payload.get("merchant_id")
        customer_id = payload.get("customer_id")
        now_iso = payload.get("received_at", utc_now_iso())
        convo = self.conversations.get(conversation_id)

        if contains_any(message, HOSTILE_PATTERNS):
            if convo:
                convo.ended = True
            return 200, {"action": "end", "rationale": "Recipient explicitly asked to stop, so the conversation is ended immediately."}

        if contains_any(message, AUTO_REPLY_PATTERNS):
            if convo:
                convo.auto_reply_count += 1
                if convo.auto_reply_count >= 1:
                    convo.ended = True
            return 200, {"action": "end", "rationale": "Detected an automated reply pattern and stopped to avoid burning turns."}

        if contains_any(message, LATER_PATTERNS):
            if convo:
                convo.last_turn = max(convo.last_turn, int(payload.get("turn_number", 1)))
            return 200, {"action": "wait", "wait_seconds": 1800, "rationale": "Recipient asked for time, so the bot backs off for 30 minutes."}

        if contains_any(message, NEGATIVE_PATTERNS) and not contains_any(message, POSITIVE_PATTERNS):
            if convo:
                convo.ended = True
            return 200, {"action": "end", "rationale": "Negative response received, so the bot exits cleanly without further nudges."}

        if convo:
            convo.last_turn = max(convo.last_turn, int(payload.get("turn_number", 1)))
            convo.last_message_at = now_iso
            if contains_any(message, POSITIVE_PATTERNS) or re.fullmatch(r"[12]", message):
                body = self._follow_up_for_conversation(convo, message)
                convo.ended = False
                return 200, {"action": "send", "body": body, "cta": "open_ended", "rationale": "Recipient signaled intent, so the bot moved straight into the next concrete step."}
            body = self._contextual_reply(convo, merchant_id, customer_id, message)
            return 200, {"action": "send", "body": body, "cta": "open_ended", "rationale": "Reply routed to the most likely next helpful step based on the active conversation."}

        if contains_any(message, POSITIVE_PATTERNS):
            merchant = self.get_payload("merchant", merchant_id)
            category = self.get_payload("category", merchant.get("category_slug")) if merchant else {}
            greeting = merchant_display_name(merchant or {}, category or {})
            body = (
                f"Done, {greeting}. I’ll move this into action mode and keep it specific. "
                f"Next I can draft the exact message/post for approval here."
            )
            return 200, {"action": "send", "body": body, "cta": "open_ended", "rationale": "Detected clear commitment and avoided further qualifying questions."}

        return 200, {"action": "wait", "wait_seconds": 900, "rationale": "No active conversation context matched, so the bot waits instead of guessing."}

    def _follow_up_for_conversation(self, convo: ConversationState, message: str) -> str:
        trigger = self.get_payload("trigger", convo.trigger_id) or {}
        if convo.scope == "customer" and re.fullmatch(r"[12]", message):
            slots = trigger.get("payload", {}).get("available_slots", [])
            index = int(message) - 1
            if 0 <= index < len(slots):
                return f"Perfect — I’ve noted {slots[index].get('label')}. We’ll confirm that slot shortly."
        if convo.composed.follow_up_body:
            return convo.composed.follow_up_body
        next_step = convo.composed.next_step_label or "next step"
        return f"Done. I’ll move ahead with the {next_step} and keep it short here."

    def _contextual_reply(self, convo: ConversationState, merchant_id: str, customer_id: Optional[str], message: str) -> str:
        trigger = self.get_payload("trigger", convo.trigger_id) or {}
        merchant = self.get_payload("merchant", merchant_id) or {}
        category = self.get_payload("category", merchant.get("category_slug")) or {}
        if trigger.get("kind") == "curious_ask_due":
            return f"Useful. I’ll turn '{message}' into one Google post and one saved reply so you can use it immediately."
        if convo.scope == "customer":
            return "Thanks. Share the time that works best and we’ll keep the next step simple."
        greeting = merchant_display_name(merchant, category)
        return f"Thanks, {greeting}. I’ll keep the next suggestion specific to '{message}' and avoid overcomplicating it."


class VeraRequestHandler(BaseHTTPRequestHandler):
    server_version = "VeraBot/0.1"

    @property
    def bot(self) -> VeraBotState:
        return self.server.bot_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8")), None
        except json.JSONDecodeError as exc:
            return None, str(exc)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._write_html(200, render_homepage())
            return
        if path == "/demo/scenarios":
            self._write_json(200, {"scenarios": self.bot.get_demo_scenarios()})
            return
        if path == "/v1/healthz":
            self._write_json(
                200,
                {
                    "status": "ok",
                    "uptime_seconds": int(time.time() - self.bot.started_at),
                    "contexts_loaded": self.bot.contexts_loaded(),
                },
            )
            return
        if path == "/v1/metadata":
            self._write_json(200, self.bot.metadata)
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload, err = self._read_json()
        if err:
            self._write_json(400, {"accepted": False, "reason": "invalid_json", "details": err})
            return
        if path == "/v1/context":
            required = {"scope", "context_id", "version", "payload", "delivered_at"}
            missing = sorted(required - set(payload.keys()))
            if missing:
                self._write_json(400, {"accepted": False, "reason": "missing_fields", "details": missing})
                return
            status, response = self.bot.store_context(
                payload["scope"],
                payload["context_id"],
                int(payload["version"]),
                payload["payload"],
                payload["delivered_at"],
            )
            self._write_json(status, response)
            return
        if path == "/v1/tick":
            now_iso = payload.get("now", utc_now_iso())
            available = payload.get("available_triggers", [])
            if not isinstance(available, list):
                self._write_json(400, {"actions": [], "reason": "available_triggers_must_be_list"})
                return
            self._write_json(200, self.bot.tick(now_iso, available))
            return
        if path == "/v1/reply":
            self._write_json(*self.bot.reply(payload))
            return
        if path == "/demo/start":
            scenario_id = payload.get("scenario_id", "")
            self._write_json(*self.bot.start_demo_conversation(scenario_id))
            return
        if path == "/demo/reply":
            conversation_id = payload.get("conversation_id", "")
            message = payload.get("message", "")
            self._write_json(*self.bot.demo_reply(conversation_id, message))
            return
        self._write_json(404, {"error": "not_found"})


def run_server(host: str, port: int, metadata_path: Optional[Path] = None) -> None:
    metadata = None
    if metadata_path and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())
    state = VeraBotState(metadata=metadata)
    server = ThreadingHTTPServer((host, port), VeraRequestHandler)
    server.bot_state = state  # type: ignore[attr-defined]
    print(f"Vera bot listening on http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic Vera bot")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--metadata", type=Path, default=None)
    args = parser.parse_args()
    run_server(args.host, args.port, args.metadata)


if __name__ == "__main__":
    main()
