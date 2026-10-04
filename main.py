import datetime
import hmac
import os

from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from database import db, Event, RecurrenceTemplate, UNITS, init_db, perform_backup, now, today

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

base_dir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(base_dir, 'data')
db_path = os.path.join(data_dir, 'tasks.db')
os.makedirs(data_dir, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

init_db(app)


def clean(value, title=False):
    value = (value or '').strip()
    if not value:
        return None
    return value.title() if title else value


def positive_int(value, default=1):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def spawn_next(event):
    """Create the next occurrence of a recurring event (if it has a live template)."""
    template = db.session.get(RecurrenceTemplate, event.template_id) if event.template_id else None
    if not (template and template.is_active):
        return
    if template.recurrence_type == 'interval' and template.interval_unit in ('months', 'years') \
            and not template.calendar_day and not template.monthly_week:
        template.set_anchor_from(event.due_date)  # lock older templates to their current day
    next_date = template.calculate_next_date(event.due_date)
    if next_date:
        db.session.add(Event(
            template_id=template.id, content=template.title,
            context=event.context, label=template.label,
            assignee=template.assignee, due_date=next_date,
        ))


def detach_template(template_id):
    """Remove a template and clear every reference to it (SQLite reuses ids)."""
    Event.query.filter_by(template_id=template_id).update({'template_id': None})
    template = db.session.get(RecurrenceTemplate, template_id)
    if template:
        db.session.delete(template)


@app.route('/')
def index():
    active = Event.query.filter(Event.completed_at.is_(None)).all()

    label_counts, assignee_counts = {}, {}
    for e in active:
        if e.label:
            label_counts[e.label] = label_counts.get(e.label, 0) + 1
        key = e.assignee or 'Unassigned'
        assignee_counts[key] = assignee_counts.get(key, 0) + 1

    filter_label = request.args.get('label')
    filter_assignee = request.args.get('assignee')
    shown = active
    if filter_label:
        shown = [e for e in shown if e.label == filter_label]
    if filter_assignee:
        want = None if filter_assignee == 'Unassigned' else filter_assignee
        shown = [e for e in shown if (e.assignee or None) == want]
    shown.sort(key=lambda e: (e.due_date, e.id))

    named_assignees = sorted(a for a in assignee_counts if a != 'Unassigned')
    return render_template('index.html',
                           events=shown,
                           all_labels=sorted(label_counts.items()),
                           all_assignees=[(a, assignee_counts[a]) for a in named_assignees],
                           unassigned_count=assignee_counts.get('Unassigned', 0),
                           active_filter=filter_label,
                           active_assignee=filter_assignee,
                           today=today())


@app.route('/history')
def history():
    finished = (Event.query.filter(Event.completed_at.isnot(None))
                .order_by(Event.completed_at.desc()).limit(500).all())
    # Undo is offered for one-off tasks, and for the latest completion of each
    # recurring series (undoing an older one would leave two copies active).
    undoable, seen_series = set(), set()
    for e in finished:
        if e.template_id is None:
            undoable.add(e.id)
        elif e.template_id not in seen_series:
            seen_series.add(e.template_id)
            undoable.add(e.id)
    return render_template('history.html', events=finished, undoable=undoable)


@app.route('/undo/<int:id>', methods=['POST'])
def undo_event(id):
    """Put a completed/skipped task back on the timeline and remove the
    next occurrence that completing it created."""
    event = db.session.get(Event, id)
    if not event or event.completed_at is None:
        return redirect(url_for('history'))
    if event.template_id:
        latest = (Event.query.filter(Event.template_id == event.template_id, Event.completed_at.isnot(None))
                  .order_by(Event.completed_at.desc()).first())
        if latest.id != event.id:
            return redirect(url_for('history'))
        spawned = Event.query.filter(Event.template_id == event.template_id, Event.completed_at.is_(None),
                                     Event.created_at >= event.completed_at)
        for nxt in spawned:
            db.session.delete(nxt)
    event.completed_at = None
    event.skipped = False
    db.session.commit()
    return redirect(url_for('history'))


@app.route('/update_note/<int:id>', methods=['POST'])
def update_note(id):
    event = db.session.get(Event, id)
    if event:
        event.completion_note = clean(request.form.get('note'))
        db.session.commit()
    return redirect(url_for('history'))


@app.route('/templates')
def view_templates():
    rows = []
    for t in RecurrenceTemplate.query.order_by(RecurrenceTemplate.title).all():
        active_inst = Event.query.filter_by(template_id=t.id, completed_at=None).first()
        rows.append({
            'template': t,
            'active_instance': active_inst,
            'next_projected': t.calculate_next_date(active_inst.due_date if active_inst else None),
        })
    return render_template('templates.html', templates=rows)


@app.route('/add', methods=['POST'])
def add_event():
    content = clean(request.form.get('content'))
    due_str = request.form.get('due_date')
    if content:
        db.session.add(Event(
            content=content,
            due_date=datetime.date.fromisoformat(due_str) if due_str else today(),
            label=clean(request.form.get('label'), title=True),
        ))
        db.session.commit()
    return redirect(url_for('index'))


@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_event(id):
    event = db.session.get(Event, id)
    if not event:
        return redirect(url_for('index'))

    template = db.session.get(RecurrenceTemplate, event.template_id) if event.template_id else None

    if request.method == 'POST':
        event.content = clean(request.form.get('content')) or event.content
        event.context = clean(request.form.get('context'))
        event.label = clean(request.form.get('label'), title=True)
        event.assignee = clean(request.form.get('assignee'), title=True)

        due_str = request.form.get('due_date')
        if due_str:
            event.due_date = datetime.date.fromisoformat(due_str)

        rec_type = request.form.get('recurrence_type')
        if rec_type in ('relative', 'interval'):
            if not template:
                template = RecurrenceTemplate(title=event.content, recurrence_type=rec_type)
                db.session.add(template)
            template.title = event.content
            template.recurrence_type = rec_type
            template.label = event.label
            template.assignee = event.assignee
            template.interval_value = positive_int(request.form.get('interval_value'))
            unit = request.form.get('interval_unit')
            template.interval_unit = unit if unit in UNITS else 'days'
            template.set_anchor_from(event.due_date, request.form.get('monthly_mode', 'day'))
            db.session.flush()
            event.template_id = template.id
        elif template:
            detach_template(template.id)

        db.session.commit()
        return redirect(url_for('index'))

    labels = [r[0] for r in db.session.query(Event.label).filter(Event.label.isnot(None)).distinct()]
    assignees = [r[0] for r in db.session.query(Event.assignee).filter(Event.assignee.isnot(None)).distinct()]
    return render_template('edit.html', event=event, template=template,
                           labels=sorted(labels), assignees=sorted(assignees))


@app.route('/toggle/<int:id>', methods=['POST'])
def toggle_event(id):
    event = db.session.get(Event, id)
    if event and event.completed_at is None:  # ignore double-taps / resubmits
        event.completed_at = now()
        spawn_next(event)
        db.session.commit()
    return redirect(request.referrer or url_for('index'))


@app.route('/skip/<int:id>', methods=['POST'])
def skip_event(id):
    event = db.session.get(Event, id)
    if event and event.template_id and event.completed_at is None:
        event.completed_at = now()
        event.skipped = True
        spawn_next(event)
        db.session.commit()
    return redirect(request.referrer or url_for('index'))


@app.route('/delete/<int:id>', methods=['POST'])
def delete_event(id):
    event = db.session.get(Event, id)
    if event:
        # Deleting the live instance of a recurring task ends the series
        # (Skip is the way to drop a single occurrence).
        if event.template_id and event.completed_at is None:
            detach_template(event.template_id)
        db.session.delete(event)
        db.session.commit()
    return redirect(url_for('index'))


@app.route('/template/delete/<int:id>', methods=['POST'])
def delete_template(id):
    detach_template(id)
    db.session.commit()
    return redirect(url_for('view_templates'))


@app.route('/template/resume/<int:id>', methods=['POST'])
def resume_template(id):
    """Re-create the active instance for a template that has none."""
    t = db.session.get(RecurrenceTemplate, id)
    if t and not Event.query.filter_by(template_id=id, completed_at=None).first():
        db.session.add(Event(template_id=t.id, content=t.title, label=t.label,
                             assignee=t.assignee, due_date=t.calculate_next_date(None)))
        db.session.commit()
    return redirect(url_for('view_templates'))


@app.route('/api/backup', methods=['POST'])
def api_backup():
    expected = os.environ.get('API_BACKUP_KEY')
    if not expected:
        return jsonify({"status": "error", "message": "Backup API key not configured on server."}), 403
    provided = request.headers.get('X-Backup-Key', '')
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        return jsonify({"status": "error", "message": "Unauthorized: Invalid or missing Backup key."}), 401

    success, result = perform_backup(db_path, data_dir)
    if success:
        return jsonify({"status": "success", "message": "Database backup completed.", "file": result}), 200
    return jsonify({"status": "error", "message": "Backup failed.", "error_details": result}), 500
