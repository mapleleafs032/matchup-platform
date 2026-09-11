/**
 * Dispatch timer for the matchup platform.
 *
 * TRIGGER: time-driven, MINUTES timer, every 15 minutes.
 *
 * Two workflows on two different cadences, because they cost different things:
 *
 *   splits.yml  VSiN splits + the line they carry. Free, so it runs on the full cadence:
 *               daily during the week, hourly the day before, every 15 minutes on game day.
 *
 *   odds.yml    The Odds API. The free plan allows 500 credits a month and each call costs 3,
 *               so this stays on narrow windows. Firing it every 15 minutes would exhaust the
 *               month in about two days.
 *
 * SETUP (once):
 *   Project Settings -> Script Properties:
 *     GH_TOKEN = fine-grained GitHub token with "Actions: Read and write" on the repo
 *     GH_REPO  = "yourname/matchup-platform"
 *   Triggers -> Add trigger -> function: tick, time-driven, Minutes timer, Every 15 minutes.
 */

var TZ = "America/Chicago";

/* Odds API windows: day 0=Sun .. 6=Sat, hours are CT, [start, end). */
var ODDS_WINDOWS = {
  NFL: [{ day: 4, start: 14, end: 20 },                      // Thursday night
        { day: 0, start: 8,  end: 20, everyMinutes: 90 },    // Sunday
        { day: 1, start: 14, end: 20 }],                     // Monday night
  CFB: [{ day: 6, start: 7,  end: 19 }]                      // Saturday
};

function tick() {
  var now = new Date();
  var day    = parseInt(Utilities.formatDate(now, TZ, "u")) % 7;   // u: 1=Mon..7=Sun -> 0=Sun
  var hour   = parseInt(Utilities.formatDate(now, TZ, "H"));
  var minute = parseInt(Utilities.formatDate(now, TZ, "m"));
  dispatchSplits_(day, hour, minute);
  dispatchOdds_(day, hour, minute);
}

/* Splits: free source, so resolution is limited only by politeness. */
function dispatchSplits_(day, hour, minute) {
  var gameDay   = (day === 0 || day === 4 || day === 6);   // Sun, Thu, Sat
  var dayBefore = (day === 3 || day === 5);                // Wed, Fri
  var fire = false;
  var why  = "";
  if (gameDay && hour >= 8 && hour <= 23)      { fire = true; why = "game day, 15 min"; }
  else if (dayBefore && minute < 15)           { fire = true; why = "day before, hourly"; }
  else if (hour === 7 && minute < 15)          { fire = true; why = "daily baseline"; }
  if (!fire) return;
  dispatchWorkflow_("splits.yml", { league: "BOTH", dry_run: "false", explain: "" }, why);
}

/* Odds API: budget-limited, so only inside the narrow windows above. */
function dispatchOdds_(day, hour, minute) {
  var leagues = [];
  Object.keys(ODDS_WINDOWS).forEach(function (lg) {
    ODDS_WINDOWS[lg].forEach(function (w) {
      if (w.day !== day || hour < w.start || hour >= w.end) return;
      var mins = (hour - w.start) * 60 + minute;
      var step = w.everyMinutes || 60;
      if (mins % step >= 15) return;            // one fire per step, not every 15 minutes
      leagues.push(lg);
    });
  });
  if (!leagues.length) return;
  var league = leagues.length === 2 ? "BOTH" : leagues[0];
  dispatchWorkflow_("odds.yml", { league: league, force: "false" }, "odds window");
}

function dispatchWorkflow_(workflow, inputs, why) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GH_TOKEN"), repo = props.getProperty("GH_REPO");
  if (!token || !repo) throw new Error("Set GH_TOKEN and GH_REPO in Script Properties");
  var res = UrlFetchApp.fetch(
    "https://api.github.com/repos/" + repo + "/actions/workflows/" + workflow + "/dispatches", {
      method: "post",
      contentType: "application/json",
      headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json" },
      payload: JSON.stringify({ ref: "main", inputs: inputs }),
      muteHttpExceptions: true
    });
  Logger.log(workflow + " (" + why + ") -> HTTP " + res.getResponseCode());
  if (res.getResponseCode() >= 300) Logger.log(res.getContentText());
}

/** Run by hand to confirm the token works. Costs one splits pull, which is free. */
function testSplits() { dispatchWorkflow_("splits.yml", { league: "NFL", dry_run: "true", explain: "" }, "manual test"); }
