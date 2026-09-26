import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from browser import Browser
from library import Library, load_learned_answers, record_answer_outcome
from settings import (ROOT, SITE, STATE, SUBMISSION_TIMEOUT, ACCOUNT_UID,
                      DATABASE_NAME, DAILY_RETRY_LIMIT, DAILY_RUN_TIMEOUT, LEARNED_ANSWERS_NAME, CHECKIN_MOOD_RANDOM,
                      CHECKIN_MOOD_DEFAULT, MOOD_PHRASES_FILE, MOOD_PHRASE_RECENT_DAYS)
from contracts import (ACTIONS, REWARD_TITLES, ActionStatus, RunStatus, format_error, recovery_summary,
                       ResumeDecision, DAILY_RETRY_ERRORS, day_complete)
from rules import choose_answer, choose_mood, choose_phrase, load_mood_phrases, site_day, verify_reward, draw_daily_due_at


def _daily_plan(day, kind, create):
    db = None
    try:
        db = Library(STATE / DATABASE_NAME)
        return db.daily_plan(ACCOUNT_UID, day, kind, create)
    except Exception:
        raise RuntimeError('daily_history_unavailable') from None
    finally:
        if db is not None:
            db.close()


def _recent_checkins():
    """Yesterday's mood and the phrases of the last MOOD_PHRASE_RECENT_DAYS days, newest first."""
    db = None
    try:
        db = Library(STATE / DATABASE_NAME)
        return db.recent_checkins(ACCOUNT_UID, MOOD_PHRASE_RECENT_DAYS)
    except Exception:
        raise RuntimeError('daily_history_unavailable') from None
    finally:
        if db is not None:
            db.close()


def _day_history(day):
    db = None
    try:
        db = Library(STATE / DATABASE_NAME)
        days = db.daily_history(ACCOUNT_UID, day, 1)['days']
        return days[0] if days else None
    except Exception:
        raise RuntimeError('daily_history_unavailable') from None
    finally:
        if db is not None:
            db.close()


def _record_heartbeat(now):
    """Liveness is recorded, never inferred: a healthy day exits quietly and writes no run at all."""
    db = None
    try:
        db = Library(STATE / DATABASE_NAME)
        db.record_heartbeat(ACCOUNT_UID, now.isoformat())
    except Exception:
        pass  # Never let bookkeeping stop the day's work; a missing beat reads as stale, not as healthy.
    finally:
        if db is not None:
            db.close()


def resume_daily(supplied_answer=None, expected_question=None):
    now = datetime.now(timezone.utc)
    day = site_day(now)
    result = {'status': RunStatus.COMPLETE, 'site_day': day, 'due_at': None,
              'decision': None, 'history': None, 'attempts': [], 'error': None}
    try:
        if not ACCOUNT_UID:
            raise RuntimeError('account_not_configured')
        _record_heartbeat(now)
        plan = _daily_plan(day, 'schedule', lambda: {'due_at': draw_daily_due_at(now).isoformat()})
        due = datetime.fromisoformat(plan['due_at'])
        result['due_at'] = plan['due_at']
        if now < due:
            result['decision'] = ResumeDecision.NOT_DUE
            return result
        result['history'] = _day_history(day)
        if day_complete(result['history']):
            if site_day() != day:
                raise RuntimeError('site_day_changed')
            result['decision'] = ResumeDecision.ALREADY_COMPLETE
            return result
        for _ in range(DAILY_RETRY_LIMIT + 1):
            if site_day() != day:
                raise RuntimeError('site_day_changed')
            attempt = run_daily(supplied_answer=supplied_answer, expected_question=expected_question,
                                _expected_day=day)
            result['decision'] = ResumeDecision.EXECUTED
            result['attempts'].append(attempt)
            result.update(status=attempt['status'], error=attempt.get('error'))
            if (attempt.get('error') not in DAILY_RETRY_ERRORS
                    or any(row['status'] == ActionStatus.SUBMISSION_UNCONFIRMED for row in attempt['actions'])):
                break
    except Exception as error:
        result.update(status=RunStatus.FAILED, error=format_error(error))
    return result


def run_daily(status_only=False, supplied_answer=None, expected_question=None, *, _expected_day=None):
    started = datetime.now(timezone.utc)
    result = {'run_id': uuid4().hex, 'started_at': started.isoformat(), 'site_day': site_day(started),
              'status_only': status_only, 'actions': []}
    browser = None
    submission_checks = 0
    missing_responses = set()

    def checkpoint():
        db = None
        try:
            db = Library(STATE / DATABASE_NAME)
            db.save_daily({**result, 'status': result.get('status', RunStatus.NEEDS_ATTENTION),
                           'finished_at': datetime.now(timezone.utc).isoformat()}, ACCOUNT_UID, checkpoint=True)
        except Exception:
            raise RuntimeError('daily_history_write_failed') from None
        finally:
            if db is not None:
                db.close()

    def check_day():
        if site_day() != result['site_day']:
            raise RuntimeError('site_day_changed')

    def read_account():
        account = browser.account()
        check_day()
        result['after'] = account
        return account

    def submit(entry, label):
        check_day()
        result['actions'].append(entry)
        try:
            checkpoint()  # Durable before the browser may send anything, including if this process is killed.
            check_day()  # A database lock may have kept us waiting across midnight.
        except Exception:
            result['actions'].remove(entry)  # The browser has not been called yet.
            checkpoint()
            raise
        try:
            browser.click_text(label)
        except RuntimeError as error:
            if str(error) == 'button_not_ready':
                result['actions'].remove(entry)  # The browser explicitly did not click.
                checkpoint()
            raise

    def observe_submission(entry, account, logs):
        spec = next(item for item in ACTIONS if item.key == entry['action'])
        completed = bool(account['app_status'].get(spec.flag))
        rewarded = verify_reward(account['uid'], spec.key, logs, now=started)
        observed_rewards = result.setdefault('reward_verified', {})
        observed_rewards[spec.key] = bool(observed_rewards.get(spec.key) or rewarded)
        entry.update(completed=completed, rice_after=account['rice'],
                     status=ActionStatus.REWARD_VERIFIED if completed and rewarded else
                            ActionStatus.REWARD_UNCONFIRMED if completed or rewarded else
                            ActionStatus.SUBMISSION_UNCONFIRMED)
        return completed and rewarded

    try:
        if _expected_day is not None and result['site_day'] != _expected_day:
            raise RuntimeError('site_day_changed')
        learned = load_learned_answers(STATE / LEARNED_ANSWERS_NAME)
        # Proven on this account beats a third-party snapshot, so learned entries win.
        bank = json.loads((ROOT / 'answers.json').read_text(encoding='utf-8')) | learned['verified']
        browser = Browser(deadline=DAILY_RUN_TIMEOUT)
        with browser:
            history = _day_history(result['site_day'])
            previous = {row['action']: row for row in history['actions']} if history else {}
            before = read_account()
            result['before'] = before
            asked = answered = None
            for action_spec in ACTIONS:
                action, flag, route = action_spec.key, action_spec.flag, action_spec.route
                account = read_account()
                prior = previous.get(action, {})
                if account['app_status'].get(flag) or prior.get('completed') or prior.get('reward_verified'):
                    known_complete = bool(account['app_status'].get(flag) or prior.get('completed'))
                    result['actions'].append({'action': action, 'completed': known_complete,
                        'status': ActionStatus.ALREADY_DONE if known_complete else ActionStatus.REWARD_UNCONFIRMED})
                    continue
                if prior.get('unconfirmed_submission_run_id'):
                    # Missing receipts cannot prove the server did not execute a request.
                    result['actions'].append({'action': action, 'status': ActionStatus.SUBMISSION_UNCONFIRMED})
                    continue
                if status_only:
                    result['actions'].append({'action': action, 'status': ActionStatus.NOT_DONE})
                    continue
                browser.goto(SITE + '/next/' + route)
                entry = {'action': action, 'status': ActionStatus.SUBMISSION_UNCONFIRMED}
                if action == 'checkin':
                    # On by default (issue #15): the mood and a line from the shipped pool are chosen like
                    # a member would and recorded with the run. Switched off, the default mood publishes nothing.
                    mood, phrase = CHECKIN_MOOD_DEFAULT, None
                    if CHECKIN_MOOD_RANDOM:
                        recent = _recent_checkins()
                        mood = choose_mood(recent[0]['mood'] if recent else None)
                        phrase = choose_phrase(mood, load_mood_phrases(MOOD_PHRASES_FILE),
                                               [row['phrase'] for row in recent if row.get('phrase')])
                    entry.update(mood=mood, phrase=phrase)
                    browser.wait_for("[...document.querySelectorAll('button')].some(e=>e.textContent.includes(" + json.dumps(mood) + "))", allow_solver=False)
                    browser.evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.includes(" + json.dumps(mood) + ")).click()")
                    if phrase is None:
                        browser.wait_for("!document.querySelector('textarea[name=todaysay]')", allow_solver=False)
                    else:
                        browser.wait_for("!!document.querySelector('textarea[name=todaysay]')", allow_solver=False)
                        filled = browser.evaluate("(()=>{const t=document.querySelector('textarea[name=todaysay]');"
                            "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(t," + json.dumps(phrase) + ");"
                            "t.dispatchEvent(new Event('input',{bubbles:true}));t.dispatchEvent(new Event('change',{bubbles:true}));"
                            "return t.value===" + json.dumps(phrase) + ";})()")
                        if not filled:
                            raise RuntimeError('checkin_phrase_not_accepted')
                    submit(entry, '提交签到')
                else:
                    data = browser.rpc('dailyQuestion.get')
                    question = data['question']
                    options = {key: value for key, value in question.items() if key.startswith('a') and key[1:].isdigit()}
                    if supplied_answer is not None:
                        if expected_question != question['qc']:
                            raise RuntimeError('question_changed_or_not_confirmed')
                        choice = choose_answer(question['qc'], options, {question['qc']: supplied_answer})
                    else:
                        choice = choose_answer(question['qc'], options, bank)
                    if choice is not None and options[choice] in learned['rejected'].get(question['qc'], []):
                        choice = None  # Already proven wrong on this account; do not pay rice twice.
                    if choice is None:
                        result['actions'].append({'action': action, 'status': ActionStatus.ANSWER_NEEDED, 'question': question['qc'], 'options': options})
                        continue
                    asked, answered = question['qc'], options[choice]
                    browser.wait_for("[...document.querySelectorAll('button')].some(e=>e.textContent.trim()===" + json.dumps(options[choice]) + ")", allow_solver=False)
                    browser.click_text(options[choice])
                    browser.wait_for("[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='提交答案'&&!e.disabled)", allow_solver=False)
                    submit(entry, '提交答案')
                # Do not navigate until the real mutation response has arrived.
                path = '/trpc/' + action_spec.method
                deadline = time.monotonic() + SUBMISSION_TIMEOUT
                solver_attempted = False
                while path not in browser.ids and time.monotonic() < deadline:
                    browser.sb.sleep(0.8)
                    if not solver_attempted and deadline - time.monotonic() < 30:
                        try:
                            browser.sb.solve_captcha()
                            browser.solver_calls += 1
                        except Exception:
                            pass
                        solver_attempted = True
                entry['response_seen'] = path in browser.ids
                checkpoint()
                if path not in browser.ids:
                    submission_checks += 1
                    missing_responses.add(action)
                    read_account()
                logs = browser.credit_logs()
                after = read_account()
                confirmed = observe_submission(entry, after, logs)
                checkpoint()
                if action == 'quiz' and asked:
                    # After observe_submission, never before: it is what establishes the receipt.
                    record_answer_outcome(asked, answered, STATE / LEARNED_ANSWERS_NAME,
                                          rewarded=bool(result.get('reward_verified', {}).get('quiz')),
                                          completed=bool(after['app_status'].get(flag)),
                                          response_seen=entry.get('response_seen', False))
                if not confirmed:
                    break
            result['after'] = read_account()
            result['reward_logs'] = [row for row in browser.credit_logs() if row.get('details', {}).get('title') in REWARD_TITLES][:10]
            check_day()
            result['solver_calls'] = browser.solver_calls
            result['business_posts_observed'] = sum(row['path'] in ['/trpc/' + action.method for action in ACTIONS] for row in browser.responses)
            result['reward_verified'] = {action.key: bool(previous.get(action.key, {}).get('reward_verified')
                or result.get('reward_verified', {}).get(action.key)
                or verify_reward(result['after']['uid'], action.key, result['reward_logs'], now=started)) for action in ACTIONS}
            for entry in result['actions']:
                if entry['status'] == ActionStatus.SUBMISSION_UNCONFIRMED:
                    observe_submission(entry, result['after'], result['reward_logs'])
            result['status'] = RunStatus.COMPLETE if (all(result['after']['app_status'].get(action.flag) for action in ACTIONS) and all(result['reward_verified'].values())) else RunStatus.NEEDS_ATTENTION
            pending = next((row for row in result['actions'] if row['status'] == ActionStatus.SUBMISSION_UNCONFIRMED), None)
            unconfirmed_response = next((spec.key for spec in ACTIONS if spec.key in missing_responses
                                          and not result['after']['app_status'].get(spec.flag)), None)
            if pending:
                result.update(status=RunStatus.FAILED, error=pending['action'] + '_submission_unconfirmed')
            elif unconfirmed_response:
                result.update(status=RunStatus.FAILED, error=unconfirmed_response + '_submission_unconfirmed')
            elif any(not result['after']['app_status'].get(action.flag) and
                     (previous.get(action.key, {}).get('completed') or result['reward_verified'][action.key]) for action in ACTIONS):
                result['error'] = 'daily_history_conflict'
    except Exception as error:
        result['status'] = RunStatus.FAILED
        result['error'] = format_error(error)
    result['recovery'] = recovery_summary(browser.read_retries if browser else 0, submission_checks)
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    result['history_saved'] = False
    db = None
    try:
        db = Library(STATE / DATABASE_NAME)
        db.save_daily(result, ACCOUNT_UID, checkpoint=True)
        result['history_saved'] = True
    except Exception:
        result['status'] = RunStatus.FAILED
        result['error'] = 'daily_history_write_failed'
    finally:
        if db is not None:
            db.close()
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / 'latest-daily.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
