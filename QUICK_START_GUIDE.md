# rTMS Side-Effect Check - Quick Start Guide

> NOTE (2025-12-18): The old AJAX save/print flow is removed.
> Current behavior is PRG (POST→Redirect→GET). The widget file is `rtms_app/static/rtms_app/side_effect_widget_v2.js`.

## User Workflow

### 1. Recording a Treatment Session

**Navigate to:** `/app/patient/{patient_id}/treatment/add/`

#### Form Layout (Top to Bottom)
1. **Date/Time & Safety Section** (Left column)
   - Treatment Date: Auto-filled with today
   - Start Time: Auto-filled with current time
   - Three safety toggles (sleep, alcohol/caffeine, medication changes)

2. **Treatment Parameters Section** (Right column)
   - **Latest Mapping Info Box** (read-only)
     - Shows: Resting MT %, Stimulation site from latest mapping
     - Shows: 3-week evaluation result (if available)
   - **Today's Settings** (user-editable)
     - Coil: Default "H1" (double-cone)
     - Site: Default "左背外側前頭前野" (left DLPFC)
     - MT%: Auto-filled from latest mapping
     - Intensity%: Auto-filled (default = MT% or 120%)
     - Frequency (Hz): Default 18.0
     - Stimulation time (s): Default 2.0
     - Pause time (s): Default 20.0
     - Total pulses: Default 1980 (calculated)
     - Sessions/day: Default 1
     - Notes: Free text field for special observations

3. **Side-Effect Check Section** (Bottom)
   - 11 side-effect items in a table
   - For each item, choose:
     - **Severity** (left 3 buttons): None / Mild / Moderate-Severe
     - **Relatedness** (right 3 buttons): Low / Present / High
   - Selected buttons highlighted in color
   - All default to "None" state
   - Changes sync instantly to hidden JSON field
   - Legend provided for reference

#### Saving
- **Save Button** (green, floating action)
  - Saves treatment session with all parameters
  - Saves side-effect check
  - Returns to dashboard
  - Shows success toast message

- **Print Button** (blue, floating action)
  - Saves treatment session first
  - Opens printable side-effect check in new window
  - Ready to print to PDF or paper

- **Back Button** (gray, floating action)
  - Abandons unsaved changes
  - Returns to dashboard

### 2. Printing a Side-Effect Check

**Two ways to access:**

**Option A: From treatment_add form**
- Click "Print" button while entering data
- Automatically saves the session
- Opens print page in new window

**Option B: Direct link**
- Navigate to: `/app/patient/{patient_id}/print/side_effect/{session_id}/`
- View saved side-effect check from past session

**Print Page Features:**
- Header: Patient name, ID, session date
- 3x3 grid: Treatment parameters for reference
- Table: 11 side-effects with severity/relatedness columns
- Memo section: Any handling notes or special instructions
- Signature line: Attending physician name/stamp
- Footer: Date issued, ready for PDF export

**To Export PDF:**
1. Navigate to print page
2. Browser menu: Print (Ctrl+P or Cmd+P)
3. Destination: "Save as PDF"
4. Save to patient folder with date: `Patient_ID_YYYY-MM-DD_SideEffects.pdf`

## Data Structure (For Developers)

### Side-Effect Rows JSON Format
```json
[
  {
    "item": "頭痛",
    "severity": 0,
    "relatedness": 0
  },
  {
    "item": "痙攣発作",
    "severity": 1,
    "relatedness": 2
  }
]
```

**Fields:**
- `item` (string): Side-effect name (fixed list of 11)
- `severity` (0-2): 0=None, 1=Mild, 2=Moderate-Severe
- `relatedness` (0-2): 0=Low probability, 1=Possible, 2=Highly related

### SideEffectCheck Model
```python
SideEffectCheck {
  session: TreatmentSession (OneToOne)
  rows: JSON array (see format above)
  memo: TextField (handling notes, max 10000 chars)
  physician_signature: CharField (attending physician name, max 128)
  updated_at: DateTimeField (auto-updated on save)
}
```

### TreatmentSession Updated Fields
```python
TreatmentSession {
  coil_type: CharField           # Coil model (e.g., "H1")
  target_site: CharField         # Stimulation target (e.g., "左背外側前頭前野")
  mt_percent: PositiveSmallInt   # Motor threshold (%)
  intensity_percent: PositiveSmallInt  # Stimulation intensity (%MT)
  frequency_hz: Decimal          # Stimulation frequency (Hz)
  train_seconds: Decimal         # Duration of stimulation (seconds)
  intertrain_seconds: Decimal    # Pause between trains (s)
  total_pulses: PositiveInt      # Total pulses delivered
  sessions_per_day: PositiveSmallInt  # Sessions in one day
  treatment_notes: TextField     # Free text notes
  motor_threshold: IntegerField  # DEPRECATED (kept for compatibility)
  intensity: IntegerField        # DEPRECATED (kept for compatibility)
}
```

## API Reference (Legacy: AJAX)

### POST /app/patient/{id}/treatment/add/
**Request Headers:**

Legacy only. Current flow does not require `X-Requested-With`.

**Form Data:**
```
csrf_middleware_token: [token]
treatment_date: YYYY-MM-DD
treatment_time: HH:MM
safety_sleep: on/off
safety_alcohol: on/off
safety_meds: on/off
coil_type: H1
target_site: 左背外側前頭前野
mt_percent: 65
intensity_percent: 120
frequency_hz: 18.0
train_seconds: 2.0
intertrain_seconds: 20.0
total_pulses: 1980
sessions_per_day: 1
treatment_notes: (optional)
side_effect_rows_json: [{"item":"...","severity":0,"relatedness":0},...]
side_effect_memo: (optional text)
side_effect_signature: (optional physician name)
action: "" or "print_side_effect"
```

**Success Response:**

Legacy only. Current flow redirects (no JSON response).
```json
{
  "status": "success",
  "id": 12345,
  "redirect_url": "/app/dashboard/?date=2025-12-17",
  "print_url": "/app/patient/1/print/side_effect/12345/"
}
```

**Error Response:**
```json
{
  "status": "error",
  "errors": {
    "field_name": ["Error message"],
    "field_name2": ["Error message"]
  }
}
```

## Configuration & Customization

### Change Default Parameters
**File:** `rtms_app/forms.py` - `TreatmentForm.Meta.fields` section

Modify `initial_data` dict in `treatment_add` view:
```python
initial_data = {
    'frequency_hz': 10.0,  # Change frequency
    'train_seconds': 3.0,  # Change stimulation time
    # ... etc
}
```

### Add New Side-Effect Items
**File:** `rtms_app/services/side_effect_schema.py`

Add to `SIDE_EFFECT_ITEMS` list:
```python
SIDE_EFFECT_ITEMS = [
    # ... existing items
    {
        'key': 'new_effect',
        'label': '新しい副作用',
    },
]
```

### Modify Print Template
**File:** `rtms_app/templates/rtms_app/print/side_effect_check.html`

- Edit CSS in `<style>` section for layout
- Edit parameter grid: `se-print-params` div
- Edit side-effect table: `se-print-table` element

### Change Button Colors
**File:** `rtms_app/static/rtms_app/side_effect_widget_v2.js`

Search for button styling in `renderRow()` method:
```javascript
// Severity buttons (currently green)
'background: #198754; color: white;'  // Change #198754 to new color

// Relatedness buttons (currently blue)
'background: #0d6efd; color: white;'  // Change #0d6efd to new color
```

## Troubleshooting

### Widget not appearing on treatment_add page
1. Check browser console for JS errors
2. Verify `side_effect_widget_v2.js` is loaded (check Network tab)
3. Ensure `#sideEffectWidget` div exists in template
4. Check `data-initial` attribute has valid JSON

### Print page showing "Recording not found"
1. Verify session ID in URL is correct
2. Check SideEffectCheck was created (check database)
3. Look for foreign key errors in server logs

### Parameters not defaulting to mapping MT%
1. Verify patient has MappingSession records
2. Check `get_latest_mt_percent()` service function
3. Confirm mapping date is recent (within treatment timeframe)

### (Legacy) AJAX save not working
1. This flow is removed; use normal POST→Redirect
2. Verify CSRF token in form
3. Check browser console for 403/400/500 errors

### Print layout broken or misaligned
1. Test in different browsers (Chrome, Firefox, Safari)
2. Check print CSS media queries
3. Adjust `@media print` rules in template
4. Verify Bootstrap is loaded correctly

## Performance Notes

- Side-effect widget renders 11 items × 6 buttons = 66 DOM elements
- JSON parsing/stringification on every button click (minimal impact)
- Print page queries 1 TreatmentSession + 1 SideEffectCheck record
- No pagination needed (single session per print)

## Security Considerations

- All side-effect data tied to authenticated user's patient
- Print view requires login + patient access permission
- Form submission requires valid CSRF token
- SideEffectCheck creation audited via AuditLog (if enabled)
- Physician signature is just a name field (not cryptographic)

## Browser Compatibility

- Modern browsers (Chrome 90+, Firefox 88+, Safari 14+)
- Mobile-responsive (tested on iOS 14+, Android 10+)
- Button styling uses flexbox (no IE11 support)
- JSON.stringify/parse widely supported

---
**Last Updated:** December 17, 2025
**Version:** 1.0
