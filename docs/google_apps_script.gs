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
        event.deleteEvent();
      }
      return jsonResponse({ ok: true, google_event_id: null });
    }

    if (event) {
      event.setTitle(payload.summary || 'BotReminder');
      event.setTime(start, end);
      event.setDescription(payload.description || '');
    } else {
      event = calendar.createEvent(payload.summary || 'BotReminder', start, end, {
        description: payload.description || '',
      });
    }

    event.setTag('botreminder_id', botEventId);
    event.removeAllReminders();
    (payload.reminders || []).slice(0, 5).forEach(function(minutes) {
      event.addPopupReminder(Number(minutes));
    });

    return jsonResponse({ ok: true, google_event_id: event.getId() });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err && err.message ? err.message : err) });
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
