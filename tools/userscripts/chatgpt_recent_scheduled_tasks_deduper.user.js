// ==UserScript==
// @name         ChatGPT Recent Scheduled Tasks Deduper
// @namespace    local
// @version      1.3
// @description  「最近の項目」内でCompany News Monitor Task01〜08とSector Weekly Workerの重複表示を畳む
// @match        https://chatgpt.com/*
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  const COMPANY_TASK_RE = /Company News Monitor Task0[1-8]/;
  const SECTOR_TASK_RE = /Sector Weekly Worker/;

  function normalize(text) {
    return (text || '').replace(/\s+/g, ' ').trim();
  }

  function getTaskKey(text) {
    const normalized = normalize(text);
    const company = normalized.match(COMPANY_TASK_RE);

    if (company) {
      return company[0];
    }

    if (SECTOR_TASK_RE.test(normalized)) {
      return 'Sector Weekly Worker';
    }

    return null;
  }

  function getTaskRow(el, section) {
    const row =
      el.closest('[role="listitem"]') ||
      el.closest('li');

    if (row && row !== section && section.contains(row)) {
      return row;
    }

    return el;
  }

  function findRecentItemsSection() {
    const nav = document.querySelector('nav');
    if (!nav) return null;

    const heading = [...nav.querySelectorAll('*')].find(
      (el) => normalize(el.textContent) === '最近の項目'
    );

    if (!heading) return null;

    let current = heading.parentElement;

    while (current && current !== nav) {
      const taskElements = [
        ...current.querySelectorAll('a'),
        ...current.querySelectorAll('button')
      ].filter((el) => getTaskKey(el.textContent));

      if (taskElements.length >= 2) {
        return current;
      }

      current = current.parentElement;
    }

    return null;
  }

  function restorePreviouslyHidden() {
    document
      .querySelectorAll('[data-task-dedupe-hidden="1"]')
      .forEach((el) => {
        el.style.display = '';
        delete el.dataset.taskDedupeHidden;
      });
  }

  function dedupeRecentTasks() {
    restorePreviouslyHidden();

    const section = findRecentItemsSection();
    if (!section) return;

    const candidates = [
      ...section.querySelectorAll('a'),
      ...section.querySelectorAll('button')
    ];

    const seenKeys = new Set();
    const processedRows = new Set();

    for (const el of candidates) {
      const key = getTaskKey(el.textContent);
      if (!key) continue;

      const row = getTaskRow(el, section);

      if (processedRows.has(row)) continue;
      processedRows.add(row);

      if (!seenKeys.has(key)) {
        seenKeys.add(key);
        continue;
      }

      row.style.display = 'none';
      row.dataset.taskDedupeHidden = '1';
    }
  }

  let timer = null;

  function scheduleDedupe() {
    clearTimeout(timer);
    timer = setTimeout(dedupeRecentTasks, 150);
  }

  const observer = new MutationObserver(scheduleDedupe);

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true
  });

  dedupeRecentTasks();
})();
