const BOTREMINDER_SECRET = 'PASTE_SECRET_HERE';
const DEFAULT_CALENDAR_ID = 'filipenko.roman@gmail.com';

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents || '{}');
    if (payload.secret !== BOTREMINDER_SECRET) {
      return jsonResponse({ ok: false, error: 'bad_secret' });
    }

    const calendarId = payload.calendar_id || DEFAULT_CALENDAR_ID;
    const calendar = CalendarApp.getCalendarById(calendarId);
    if (!calendar) {
      return jsonResponse({ ok: false, error: 'calendar_not_found' });
    }

    const start = new Date(payload.start_at);
    const end = new Date(payload.end_at);
    const botEventId = String(payload.bot_event_id || '');
    let event = findBotReminderEvent(calendar, payload.google_event_id, botEventId, start);

    if (payload.status === 'cancelled') {
      if (event) {
        deleteCalendarThing(event);
      }
      return jsonResponse({ ok: true, google_event_id: null });
    }

    if (payload.recurrence) {
      if (event) {
        deleteCalendarThing(event);
      }
      const recurrence = buildRecurrence(payload.recurrence, payload.timezone);
      event = calendar.createEventSeries(payload.summary || 'BotReminder', start, end, recurrence, {
        description: payload.description || '',
      });
    } else if (event) {
      event.setTitle(payload.summary || 'BotReminder');
      event.setTime(start, end);
      event.setDescription(payload.description || '');
    } else {
      event = calendar.createEvent(payload.summary || 'BotReminder', start, end, {
        description: payload.description || '',
      });
    }

    setTagIfPossible(event, 'botreminder_id', botEventId);
    removeRemindersIfPossible(event);
    (payload.reminders || []).slice(0, 5).forEach(function(minutes) {
      addReminderIfPossible(event, Number(minutes));
    });

    return jsonResponse({ ok: true, google_event_id: event.getId() });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

function buildRecurrence(recurrencePayload, timezone) {
  let recurrence = CalendarApp.newRecurrence().setTimeZone(timezone || 'Asia/Novosibirsk');
  let rule;
  if (recurrencePayload.frequency === 'weekdays') {
    rule = recurrence.addWeeklyRule().onlyOnWeekdays([
      CalendarApp.Weekday.MONDAY,
      CalendarApp.Weekday.TUESDAY,
      CalendarApp.Weekday.WEDNESDAY,
      CalendarApp.Weekday.THURSDAY,
      CalendarApp.Weekday.FRIDAY,
    ]);
  } else if (recurrencePayload.frequency === 'daily') {
    rule = recurrence.addDailyRule();
  } else if (recurrencePayload.frequency === 'biweekly') {
    rule = recurrence.addWeeklyRule().interval(2);
  } else if (recurrencePayload.frequency === 'monthly') {
    rule = recurrence.addMonthlyRule();
  } else {
    rule = recurrence.addWeeklyRule();
  }
  if (recurrencePayload.until) {
    rule.until(new Date(recurrencePayload.until));
  }
  return recurrence;
}

function deleteCalendarThing(event) {
  if (typeof event.deleteEventSeries === 'function') {
    event.deleteEventSeries();
  } else {
    event.deleteEvent();
  }
}

function setTagIfPossible(event, key, value) {
  if (typeof event.setTag === 'function') {
    event.setTag(key, value);
  }
}

function removeRemindersIfPossible(event) {
  if (typeof event.removeAllReminders === 'function') {
    event.removeAllReminders();
  }
}

function addReminderIfPossible(event, minutes) {
  if (typeof event.addPopupReminder === 'function') {
    event.addPopupReminder(minutes);
  }
}

function findBotReminderEvent(calendar, googleEventId, botEventId, start) {
  if (googleEventId) {
    try {
      const event = calendar.getEventById(googleEventId);
      if (event) {
        return event;
      }
    } catch (err) {}
    try {
      const eventSeries = calendar.getEventSeriesById(googleEventId);
      if (eventSeries) {
        return eventSeries;
      }
    } catch (err) {}
  }

  if (!botEventId || !start) {
    return null;
  }

  const from = new Date(start.getTime() - 14 * 24 * 60 * 60 * 1000);
  const to = new Date(start.getTime() + 14 * 24 * 60 * 60 * 1000);
  const events = calendar.getEvents(from, to);
  for (let i = 0; i < events.length; i += 1) {
    if (events[i].getTag('botreminder_id') === botEventId) {
      return events[i];
    }
  }
  return null;
}

function jsonResponse(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}
