/**
 * Dispatch timer for the matchup platform.
 *
 * TRIGGER: time-driven, MINUTES timer, every 15 minutes.
 *
 * VSiN (DraftKings) is the single market source: the line and the ticket/money splits arrive in the
 * same request, so they share a timestamp exactly. The pull is free, so cadence is limited only by
 * politeness:
 *
 *     game day (Thu / Sat / Sun, 8am-11pm CT)  every 15 minutes
 *     day before (Wed / Fri)                   hourly
 *     otherwise                                once daily at 7am CT
 *
 * GitHub's own cron is best-effort and often runs late, which is why this timer exists.
 *
 * SETUP (once):
 *   Project Settings -> Script Properties:
 *     GH_TOKEN = fine-grained GitHub token with "Actions: Read and write" on the repo
 *     GH_REPO  = "mapleleafs032/matchup-platform"
 *   Triggers -> Add trigger -> function: tick, time-driven, Minutes timer, Every 15 minutes.
 */

var TZ = "America/Chicago";
var SPLITS_WORKFLOW = "splits.yml";

function tick() {
  var now    = new Date();
  var day    = parseInt(Utilities.formatDate(now, TZ, "u")) % 7;   // u: 1=Mon..7=Sun -> 0=Sun
  var hour   = parseInt(Utilities.formatDate(now, TZ, "H"));
  var minute = parseInt(Utilities.formatDate(now, TZ, "m"));

  var gameDay   = (day === 0 || day === 4 || day === 6);   // Sunday, Thursday, Saturday
  var dayBefore = (day === 3 || day === 5);                // Wednesday, Friday

  var why = null;
  if (gameDay && hour >= 8 && hour <= 23)   why = "game day, every 15 minutes";
  else if (dayBefore && minute < 15)        why = "day before, hourly";
  else if (hour === 7 && minute < 15)       why = "daily baseline";

  if (!why) { Logger.log("no pull due"); return; }
  dispatchWorkflow_(SPLITS_WORKFLOW, { league: "BOTH", dry_run: "false", explain: "" }, why);
}

function dispatchWorkflow_(workflow, inputs, why) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GH_TOKEN");
  var repo  = props.getProperty("GH_REPO");
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

/** Run this by hand once to confirm the token works. Costs nothing: it triggers a dry run. */
function testSplits() {
  dispatchWorkflow_(SPLITS_WORKFLOW, { league: "NFL", dry_run: "true", explain: "" }, "manual test");
}
