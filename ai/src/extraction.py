"""
Extraction module: messy report -> structured dict.
Free/local version. Setup:
  1. Install Ollama from ollama.com
  2. ollama pull qwen2.5:7b   (use qwen2.5:3b if your laptop is weak)
  3. pip install ollama pydantic
"""
import json
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ValidationError, field_validator
import ollama

MODEL = "qwen2.5:7b"

NeedType = Literal["water", "food", "medicine", "blanket", "shelter", "transport", "other"]
Urgency = Literal["low", "medium", "high", "critical"]


class Report(BaseModel):
    language: str                                   # e.g. "en", "hi", "es"
    need_type: NeedType
    quantity: Optional[int] = None                  # items or people; None if unknown
    neighborhood: Optional[str] = None
    address: Optional[str] = None
    landmark: Optional[str] = None
    household_size: Optional[int] = None
    household_note: Optional[str] = None            # e.g. "second floor flat, family of 4"
    urgency: Urgency
    urgency_cues: List[str] = []                    # phrases that justify urgency
    accessibility_needs: List[str] = []             # wheelchair, elderly, infant...
    time_window: Optional[str] = None
    missing_fields: List[str] = []                  # fields you could not find
    confidence: dict = Field(default_factory=dict)  # field -> 0..1
    translation_en: str = ""                        # English summary

    # The model sometimes returns null for an empty list/dict (seen on a Vietnamese report).
    # Treat null as empty instead of failing the whole extraction.
    @field_validator("urgency_cues", "accessibility_needs", "missing_fields", mode="before")
    @classmethod
    def _null_list(cls, v):
        return [] if v is None else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _null_dict(cls, v):
        return {} if v is None else v

    @field_validator("translation_en", mode="before")
    @classmethod
    def _null_str(cls, v):
        return "" if v is None else v


SYSTEM_PROMPT = """You extract structured data from disaster-relief requests.
Reports may be SMS, forms, or call transcripts, in any language, messy or incomplete.

Rules:
- Output ONLY one JSON object. No prose, no markdown fences.
- Never invent information. If a field is not stated, use null (or []) and add its name to missing_fields.
- need_type must be one of: water, food, medicine, blanket, shelter, transport, other.
- medicine also covers first aid, injuries, wounds, bandages and prescription drugs such as insulin. Use other only when no listed type fits.
- urgency: critical = life threat (medical emergency, rising water, trapped); high = vulnerable people
  (infant, elderly, sick) or no supply for >24h; medium = shortage; low = non-urgent.
- quantity: number of items requested. If no item count is given but a people count is (e.g. "family of 6", "5 personas", "4 log"), use the people count. null only if there is no number at all.
- Children, infants, elderly, sick or disabled people mentioned => urgency at least "high". No food or water for 24 hours or more => at least "high".
- Always fill need_type, neighborhood and urgency when they can be inferred. If the report is vague, give your best guess and lower the confidence instead of leaving them null.
- urgency_cues: copy the exact phrases from the report that justify the urgency.
- confidence: a 0-1 score for each of: need_type, quantity, location, urgency. Lower it when the text is vague.
- household_note: any detail that tells separate households apart (floor, flat number, names of family, etc.).
- translation_en: one-sentence English summary.

JSON keys: language, need_type, quantity, neighborhood, address, landmark, household_size,
household_note, urgency, urgency_cues, accessibility_needs, time_window, missing_fields,
confidence, translation_en.

Examples:
Report: "Need 20 water bottles, Rampur colony lane 3, 2 kids and grandma can't walk. since yesterday no water"
JSON: {"language":"en","need_type":"water","quantity":20,"neighborhood":"Rampur colony","address":"lane 3","landmark":null,"household_size":4,"household_note":"2 kids and grandmother","urgency":"high","urgency_cues":["grandma can't walk","since yesterday no water"],"accessibility_needs":["elderly cannot walk"],"time_window":null,"missing_fields":["landmark","time_window"],"confidence":{"need_type":0.95,"quantity":0.9,"location":0.7,"urgency":0.8},"translation_en":"Family of 4 in Rampur colony lane 3 needs 20 water bottles; elderly member immobile."}

Report: "Medicina urgente para mi papa, diabetico, Barrio Sur cerca de la escuela"
JSON: {"language":"es","need_type":"medicine","quantity":null,"neighborhood":"Barrio Sur","address":null,"landmark":"near the school","household_size":null,"household_note":"father, diabetic","urgency":"critical","urgency_cues":["urgente","diabetico"],"accessibility_needs":[],"time_window":null,"missing_fields":["quantity","address","time_window"],"confidence":{"need_type":0.9,"quantity":0.1,"location":0.5,"urgency":0.85},"translation_en":"Urgent medicine needed for diabetic father in Barrio Sur near the school."}

Report: "chawal aur dal chahiye, Lakshmi Nagar gali 4, 5 log, kal se kuch nahi khaya"
JSON: {"language":"hi","need_type":"food","quantity":5,"neighborhood":"Lakshmi Nagar","address":"gali 4","landmark":null,"household_size":5,"household_note":null,"urgency":"high","urgency_cues":["kal se kuch nahi khaya"],"accessibility_needs":[],"time_window":null,"missing_fields":["landmark","time_window"],"confidence":{"need_type":0.9,"quantity":0.8,"location":0.8,"urgency":0.8},"translation_en":"5 people in Lakshmi Nagar gali 4 need rice and dal; no food since yesterday."}

Report: "Necesitamos mantas, Barrio Norte, somos 3, hace mucho frio"
JSON: {"language":"es","need_type":"blanket","quantity":3,"neighborhood":"Barrio Norte","address":null,"landmark":null,"household_size":3,"household_note":null,"urgency":"medium","urgency_cues":["hace mucho frio"],"accessibility_needs":[],"time_window":null,"missing_fields":["address","landmark","time_window"],"confidence":{"need_type":0.9,"quantity":0.8,"location":0.6,"urgency":0.7},"translation_en":"3 people in Barrio Norte need blankets because of the cold."}
"""


def extract(text: str, report_id: str = "") -> dict:
    """Return validated dict. Flags needs_review if confidence is low or parsing fails twice."""
    last_err = None
    for attempt in range(2):
        res = ollama.chat(
            model=MODEL,
            format="json",
            options={"temperature": 0},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Report: {text}\nJSON:" + (f"\n(Previous attempt failed: {last_err[:200]}. Return a valid JSON object only.)" if last_err else "")},
            ],
        )
        raw = res["message"]["content"].strip().replace("```json", "").replace("```", "")
        try:
            data = Report(**json.loads(raw)).model_dump()
            data["report_id"] = report_id
            data["raw_text"] = text
            data["needs_review"] = review_flag(data)
            return data
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = str(e)
    return {"report_id": report_id, "raw_text": text, "needs_review": True,
            "error": last_err}


def review_flag(d: dict, threshold: float = 0.5) -> bool:
    """Send to human coordinator if need/location/urgency confidence is low or location is missing."""
    conf = d.get("confidence", {})
    low = False
    for k in ("need_type", "location", "urgency"):
        try:
            if float(conf.get(k, 1)) < threshold:
                low = True
        except (TypeError, ValueError):
            low = True
    no_location = not (d.get("neighborhood") or d.get("address") or d.get("landmark"))
    return low or no_location


if __name__ == "__main__":
    tests = [
        "pani chahiye 10 bottle, Sector 5 gali no 2, bacche chhote hain",
        "Need blankets x6 at Green Park, flood water in house, 2nd floor flat",
        "Ayuda comida Barrio Sur",
    ]
    for i, t in enumerate(tests):
        print(json.dumps(extract(t, f"R{i}"), indent=2, ensure_ascii=False))