/**
 * Game-day odds timer. Runs on an hourly time-driven trigger and dispatches the GitHub "Odds snapshot"
 * workflow inside game-day windows. This exists because GitHub cron is best-effort; Apps Script triggers fire on time.
 *
 * SETUP (once):
 *   1. script.google.com -> New project -> paste this file.
 *   2. Project Settings -> Script Properties: add
 *        GH_TOKEN  = a fine-grained GitHub token with "Actions: Read and write" on the repo
 *        GH_REPO   = "<your-github-username>/matchup-platform"
 *   3. Triggers -> Add trigger -> function: tick, event: time-driven, hour timer, every hour.
 *
 * Windows are in America/Chicago. Free-plan budget is ~90 Odds API credits/week with these windows
 * (see Phase 2 §2.15). Paid plan: widen them and set ODDS_PLAN=paid as a GitHub repo variable.
 */
// Cadence: daily the week of the game, hourly the day before, every 15 minutes on game day.
// GitHub's own cron is best-effort and often runs late; this timer is what makes the 15-minute
// game-day resolution actually happen. Set the trigger to "every 15 minutes".
var SPLITS_WORKFLOW = "splits.yml";

var WINDOWS = {
  // day: 0=Sun ... 6=Sat ; hours are inclusive start, exclusive end (CT)
  NFL: [ {day: 4, start: 14, end: 20},   // Thu: 6h before 7:15pm CT kickoff
         {day: 0, start: 8,  end: 20, everyMinutes: 90},  // Sun: 8am–8pm CT, every 90 min (~8 pulls)
         {day: 1, start: 14, end: 20} ], // Mon
  CFB: [ {day: 6, start: 7,  end: 19} ]  // Sat: hourly 7am–7pm CT (CFBD calls, not Odds API credits)
};

function tick() {
  dispatchSplits_();   // splits + line snapshot on every fire; the job itself is cheap and append-only
  var now = new Date();
  var tz = "America/Chicago";
  var day = parseInt(Utilities.formatDate(now, tz, "u")) % 7;   // u: 1=Mon..7=Sun -> 0=Sun
  var hour = parseInt(Utilities.formatDate(now, tz, "H"));
  var minute = parseInt(Utilities.formatDate(now, tz, "m"));
  var leagues = [];
  Object.keys(WINDOWS).forEach(function (lg) {
    WINDOWS[lg].forEach(function (w) {
      if (w.day === day && hour >= w.start && hour < w.end) {
        if (w.everyMinutes && ((hour - w.start) * 60 + minute) % w.everyMinutes >= 60) return;
        leagues.push(lg);
      }
    });
  });
  if (leagues.length === 0) { Logger.log("outside windows"); return; }
  var league = leagues.length === 2 ? "BOTH" : leagues[0];
  dispatch_(league);
}

function dispatchSplits_() {
  var now = new Date(), tz = "America/Chicago";
  var day = parseInt(Utilities.formatDate(now, tz, "u")) % 7;
  var hour = parseInt(Utilities.formatDate(now, tz, "H"));
  var minute = parseInt(Utilities.formatDate(now, tz, "m"));
  var gameDay = (day === 0 || day === 4 || day === 6);            // Sun, Thu, Sat
  var dayBefore = (day === 3 || day === 5 || day === 6);          // Wed, Fri, Sat evening
  var fire = false;
  if (gameDay && hour >= 8 && hour <= 23) fire = true;            // every 15 minutes
  else if (dayBefore && minute < 15) fire = true;                 // hourly
  else if (hour === 7 && minute < 15) fire = true;                // daily baseline
  if (!fire) { return; }
  dispatchWorkflow_(SPLITS_WORKFLOW, { league: "BOTH", dry_run: "false", explain: "" });
}

function dispatchWorkflow_(wf, inputs) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GH_TOKEN"), repo = props.getProperty("GH_REPO");
  if (!token || !repo) throw new Error("Set GH_TOKEN and GH_REPO in Script Properties");
  var res = UrlFetchApp.fetch("https://api.github.com/repos/" + repo + "/actions/workflows/" + wf + "/dispatches", {
    method: "post", contentType: "application/json",
    headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json" },
    payload: JSON.stringify({ ref: "main", inputs: inputs }),
    muteHttpExceptions: true
  });
  Logger.log(wf + " -> HTTP " + res.getResponseCode());
}

function dispatch_(league) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GH_TOKEN"), repo = props.getProperty("GH_REPO");
  if (!token || !repo) throw new Error("Set GH_TOKEN and GH_REPO in Script Properties");
  var url = "https://api.github.com/repos/" + repo + "/actions/workflows/odds.yml/dispatches";
  var res = UrlFetchApp.fetch(url, {
    method: "post", contentType: "application/json",
    headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json" },
    payload: JSON.stringify({ ref: "main", inputs: { league: league, force: "false" } }),
    muteHttpExceptions: true
  });
  Logger.log("dispatch " + league + " -> HTTP " + res.getResponseCode());
  if (res.getResponseCode() >= 300) throw new Error(res.getContentText());
}

/** Run once by hand to confirm the token works. Costs one odds snapshot. */
function testDispatch() { dispatch_("NFL"); }
