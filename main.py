from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import datetime

from database import db, Event, RecurrenceTemplate, init_db, perform_backup

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

base_dir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(base_dir, 'data')
db_path = os.path.join(data_dir, 'tasks.db')
os.makedirs(data_dir, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

init_db(app)

def get_assignee_colors(unique_assignees):
    PALETTE = ['#d0bcff', '#448aff', '#69f0ae', '#ffab40', '#ff5252', '#ff80ab', '#64ffda', '#536dfe']
    colors = {}
    for i, a in enumerate(sorted(list(unique_assignees))):
        colors[a] = PALETTE[i % len(PALETTE)]
    return colors

def get_label_colors(unique_labels):
    LABEL_PALETTE = ['purple', 'blue', 'green', 'orange', 'red', 'pink', 'teal', 'indigo']
    colors = {}
    for i, lbl in enumerate(sorted(unique_labels)):
        colors[lbl] = LABEL_PALETTE[i % len(LABEL_PALETTE)]
    return colors

@app.route('/')
def index():
    all_events = Event.query.all()
    
    children_map = {}
    for e in all_events:
        if e.parent_id:
            children_map.setdefault(e.parent_id, []).append(e)

    filter_label = request.args.get('label')
    filter_assignee = request.args.get('assignee')

    label_counts, assignee_counts, unique_assignees, unique_labels = {}, {}, set(), set()
    total_active = 0
    
    for e in all_events:
        if e.completed_at is None:
            total_active += 1
            if e.label:
                label_counts[e.label] = label_counts.get(e.label, 0) + 1
                unique_labels.add(e.label)
            assgn_key = e.assignee if e.assignee else "Unassigned"
            assignee_counts[assgn_key] = assignee_counts.get(assgn_key, 0) + 1
        if e.assignee:
            unique_assignees.add(e.assignee)

    query = Event.query.filter(Event.completed_at == None)
    if filter_label:
        query = query.filter(Event.label == filter_label)
    if filter_assignee:
        if filter_assignee == 'Unassigned':
            query = query.filter((Event.assignee == None) | (Event.assignee == ''))
        else:
            query = query.filter(Event.assignee == filter_assignee)
        
    active_events = sorted(query.all(), key=lambda x: x.due_date)

    return render_template('index.html', 
                           events=active_events, 
                           all_labels=[(lbl, label_counts.get(lbl, 0)) for lbl in sorted(unique_labels)], 
                           all_assignees=[(a, assignee_counts.get(a, 0)) for a in sorted(unique_assignees)],
                           assignee_colors=get_assignee_colors(unique_assignees),
                           label_colors=get_label_colors(unique_labels),
                           unassigned_count=assignee_counts.get("Unassigned", 0),
                           active_filter=filter_label,
                           active_assignee=filter_assignee,
                           children_map=children_map,
                           total_active=total_active,
                           datetime=datetime)

@app.route('/history')
def history():
    finished_events = Event.query.filter(Event.completed_at.isnot(None)).order_by(Event.completed_at.desc()).all()
    return render_template('history.html', events=finished_events)

@app.route('/templates')
def view_templates():
    templates = RecurrenceTemplate.query.all()
    template_statuses = []
    for t in templates:
        active_inst = Event.query.filter_by(template_id=t.id, completed_at=None).first()
        next_calc = t.calculate_next_date(active_inst.due_date if active_inst else None)
        template_statuses.append({
            'template': t,
            'active_instance': active_inst,
            'next_projected': next_calc
        })
    return render_template('templates.html', templates=template_statuses)

@app.route('/add', methods=['POST'])
def add_event():
    content = request.form.get('content')
    raw_label = request.form.get('label')
    due_str = request.form.get('due_date')
    
    label = raw_label.strip().title() if raw_label else None
    due_date = datetime.date.fromisoformat(due_str) if due_str else datetime.date.today()

    if content:
        new_event = Event(content=content, due_date=due_date, label=label)
        db.session.add(new_event)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_event(id):
    event = db.session.get(Event, id)
    if not event: return redirect(url_for('index'))
    
    template = db.session.get(RecurrenceTemplate, event.template_id) if event.template_id else None

    if request.method == 'POST':
        event.content = request.form.get('content')
        event.context = request.form.get('context') or None
        
        raw_label = request.form.get('label')
        event.label = raw_label.strip().title() if raw_label else None
        
        raw_assignee = request.form.get('assignee')
        event.assignee = raw_assignee.strip().title() if raw_assignee else None

        due_str = request.form.get('due_date')
        if due_str: event.due_date = datetime.date.fromisoformat(due_str)
        
        # Epic / Subtask circular loop protection
        parent_id_raw = request.form.get('parent_id')
        if parent_id_raw and parent_id_raw.isdigit():
            pid_int = int(parent_id_raw)
            if pid_int != event.id:
                circular = False
                curr = db.session.get(Event, pid_int)
                while curr:
                    if curr.id == event.id:
                        circular = True
                        break
                    curr = db.session.get(Event, curr.parent_id) if curr.parent_id else None
                event.parent_id = None if circular else pid_int
        else:
            event.parent_id = None

        # Process Recurrence Configurations
        rec_type = request.form.get('recurrence_type')
        if rec_type and rec_type != 'none':
            if not template:
                # 1. Instantiate the object with the required field up front
                template = RecurrenceTemplate(title=event.content, recurrence_type=rec_type)
                db.session.add(template)
            
            # 2. Update all fields cleanly
            template.title = event.content
            template.recurrence_type = rec_type
            template.label = event.label
            template.assignee = event.assignee
            
            if rec_type in ['relative', 'interval']:
                template.interval_value = int(request.form.get('interval_value', 1))
                template.interval_unit = request.form.get('interval_unit', 'days')
            elif rec_type == 'calendar':
                template.calendar_month = int(request.form.get('calendar_month', 1))
                template.calendar_day = int(request.form.get('calendar_day', 1))
                
            # 3. Safely flush or let SQLAlchemy handle it at commit time
            db.session.flush()
            event.template_id = template.id
        else:
            if template:
                db.session.delete(template)
            event.template_id = None

        db.session.commit()
        return redirect(url_for('index'))

    all_events = Event.query.filter(Event.id != id, Event.completed_at == None).all()
    unique_assignees = {e.assignee for e in Event.query.all() if e.assignee}
    unique_labels = {e.label for e in Event.query.all() if e.label}

    return render_template('edit.html', event=event, template=template, all_tasks=all_events,
                           unique_assignees=sorted(list(unique_assignees)),
                           all_labels=[(l, 0) for l in sorted(unique_labels)],
                           datetime=datetime)

@app.route('/toggle/<int:id>')
def toggle_event(id):
    event = db.session.get(Event, id)
    if event:
        event.completed_at = datetime.datetime.now()
        
        # Spawn new chronological instance if blueprint rules exist
        if event.template_id:
            template = db.session.get(RecurrenceTemplate, event.template_id)
            if template and template.is_active:
                next_date = template.calculate_next_date(event.due_date)
                if next_date:
                    next_ev = Event(
                        template_id=template.id, content=template.title,
                        context=event.context, label=template.label,
                        assignee=template.assignee, due_date=next_date,
                        parent_id=event.parent_id
                    )
                    db.session.add(next_ev)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/skip/<int:id>')
def skip_event(id):
    event = db.session.get(Event, id)
    if event and event.template_id:
        event.completed_at = datetime.datetime.now()
        event.completion_note = "[SKIPPED]"
        
        template = db.session.get(RecurrenceTemplate, event.template_id)
        if template and template.is_active:
            next_date = template.calculate_next_date(event.due_date)
            if next_date:
                next_ev = Event(
                    template_id=template.id, content=template.title,
                    context=event.context, label=template.label,
                    assignee=template.assignee, due_date=next_date,
                    parent_id=event.parent_id
                )
                db.session.add(next_ev)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/delete/<int:id>')
def delete_event(id):
    event = db.session.get(Event, id)
    if event:
        db.session.delete(event)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/template/delete/<int:id>')
def delete_template(id):
    template = db.session.get(RecurrenceTemplate, id)
    if template:
        db.session.delete(template)
        db.session.commit()
    return redirect(url_for('view_templates'))

if __name__ == '__main__':
    if not os.environ.get("WERKZEUG_RUN_MAIN") and os.path.exists(db_path):
        perform_backup(db_path, data_dir)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)