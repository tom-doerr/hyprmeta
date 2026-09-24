// Mirror "a reply is generating" into the tab title, which the browser passes on
// as the window title, where hyprmeta reads it (hyprmeta/agents.py, WEB_TAG):
//   idle        "⁣" + title   (invisible separator: the tab looks unchanged)
//   generating  "⏳⁣" + title
"use strict";

const TAG = "⁣";
const RUNNING = "⏳";
// While a reply streams, the composer's send button becomes a Stop button.
const STOP = 'button[data-testid="stop-button"], #composer-submit-button[aria-label*="stop" i]';

function bare(title) {
  if (title.startsWith(RUNNING + TAG)) return title.slice(2);
  if (title.startsWith(TAG)) return title.slice(1);
  return title;
}

function apply() {
  const generating = document.querySelector(STOP) !== null;
  const want = (generating ? RUNNING + TAG : TAG) + bare(document.title);
  if (document.title !== want) document.title = want;
}

// DOM mutations keep arriving in hidden tabs, where timers get throttled to once a
// minute, so the observer is what catches the Stop button disappearing. The
// interval only repairs a title the page rewrote without a mutation we saw.
// React may keep the same <button> and only swap its attributes, hence attributeFilter.
new MutationObserver(apply).observe(document.documentElement, {
  childList: true, subtree: true, characterData: true,
  attributes: true, attributeFilter: ["data-testid", "aria-label"],
});
setInterval(apply, 2000);
apply();
