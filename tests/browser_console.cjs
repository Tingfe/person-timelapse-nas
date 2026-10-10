// Optional UI regression check. Uses synthetic records only; no NAS access.
// Start: python3 -m http.server 18879 --bind 127.0.0.1 --directory app
// Run with an existing Playwright installation: node tests/browser_console.cjs
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const path = require('node:path');
const os = require('node:os');

const dates = Array.from({length: 90}, (_, index) => {
  const day = new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10).replaceAll('-', '');
  return {date: day, total_files: 20, events: 80, ready: true,
    scan_status: index % 3 ? 'completed' : 'partial', source_cameras: ['legacy'], cameras: ['legacy'],
    timelapse_count: index % 4 ? 0 : 1};
}).reverse();
dates.unshift({date: '20260324', scan_status: 'pending', total_files: 41, source_cameras: ['0'], cameras: [], events: 0});
const timelapses = Array.from({length: 14}, (_, index) => ({
  name: `people-timelapse-legacy-2024${String(index % 12 + 1).padStart(2, '0')}01.mp4`,
  start_date: `2024${String(index % 12 + 1).padStart(2, '0')}01`,
  end_date: `2024${String(index % 12 + 1).padStart(2, '0')}01`,
  camera: 'legacy', size_bytes: 12400000, duration_seconds: 40 + index,
  created_at: '2026-01-01T00:00:00Z', status: index === 0 ? 'partial' : 'completed',
  failed_files: index === 0 ? 1 : 0,
}));
timelapses.unshift({name: 'people-timelapse-0-20231201-20250131.mp4', start_date: '20231201',
  end_date: '20250131', camera: '0', size_bytes: 64000000, duration_seconds: 240,
  status: 'completed', created_at: '2026-01-02T00:00:00Z'});
const tasks = Array.from({length: 10}, (_, index) => ({id: `scan-${index}`, kind: 'scan',
  date: `202401${String(index + 1).padStart(2, '0')}`, status: index === 0 ? 'running' : 'queued',
  progress: {percent: 25, total_files: 20, file_index: 5, current_file: 'synthetic.mp4'},
  detail: '测试任务，不读取真实录像', started_at: new Date().toISOString(),
}));
const plans = Array.from({length: 4}, (_, index) => ({id: `plan-${index}`, kind: 'timelapse',
  start_date: '20240101', end_date: '20241231', camera: 'legacy', status: index === 0 ? 'running' : 'completed',
  phase: index === 0 ? 'scanning' : 'completed', percent: index === 0 ? 55 : 100,
  current_date: '20240229', completed_days: index === 0 ? 50 : 90, total_days: 90,
  partial_days: 1, failed_days: 0, retry_ids: [], outputs: index ? [timelapses[0].name] : [],
}));
let model = {dates, timelapses, tasks, plans, queue_paused: false, queued_count: 9,
  diagnostics: {available: true, recognized_files: 10000, mp4_files: 10000}};
let created = [];

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      let body = model;
      if (pathname.startsWith('/api/date/')) {
        body = {events: {events: {legacy: Array.from({length: 120}, (_, index) => ({
          start: `2024-02-29T01:${String(index % 60).padStart(2, '0')}:00`,
          end: `2024-02-29T01:${String(index % 60).padStart(2, '0')}:10`, hits: 2,
        }))}}, exports: [timelapses[0]]};
      } else if (request.method() === 'POST' && pathname === '/api/tasks') {
        created.push(request.postDataJSON());
        body = {created: 91, scan_created: 90, dates: dates.map(day => day.date)};
      } else if (pathname === '/api/status') {
        body = {tasks: model.tasks, plans: model.plans, queue_paused: false, queued_count: 9};
      }
      await route.fulfill({json: body});
    });
    await page.route('**/media/**', route => route.fulfill({status: 200, contentType: 'video/mp4', body: ''}));
    await page.goto((process.env.CONSOLE_TEST_URL || 'http://127.0.0.1:18879') + '/web/index.html');
    await page.waitForSelector('.month');
    assert.equal(await page.locator('.month').count(), 12);
    assert.equal(await page.locator('#yearSelect').inputValue(), '2026');
    await page.selectOption('#yearSelect', '2024');
    await page.click('#selectYear');
    assert.equal(await page.locator('#dateSelect').inputValue(), '2024-01-01');
    assert.equal(await page.locator('#endDate').inputValue(), '2024-12-31');
    assert.equal(created.length, 0, 'presets must not start computation');
    await page.click('[data-month="02"]');
    assert.equal(await page.locator('.day').count(), 29);
    await page.click('#selectMonth');
    assert.equal(await page.locator('#endDate').inputValue(), '2024-02-29');
    await page.click('[data-range-target="end"]');
    await page.click('[data-date="20240220"]');
    assert.equal(await page.locator('#dateSelect').inputValue(), '2024-02-01');
    assert.equal(await page.locator('#endDate').inputValue(), '2024-02-20');
    await page.waitForSelector('#eventPager [data-page="1"]');
    await page.click('#eventPager [data-page="1"]');
    await page.waitForFunction(() => document.querySelector('#eventPager').textContent.includes('2 / 3'));
    assert.match(await page.locator('#eventPager').innerText(), /2 \/ 3/);
    assert.equal(await page.locator('.event').count(), 50);
    await page.click('#timelapse');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('计划已创建'));
    assert.equal(created[0].end_date, '20240220');
    assert.equal(created[0].camera, 'legacy');

    await page.click('#planPager [data-page="1"]');
    assert.match(await page.locator('#planPager').innerText(), /2 \/ 2/);
    await page.click('#taskPager [data-page="1"]');
    assert.equal(await page.locator('#tasks .task').count(), 2);
    assert.equal(await page.locator('#timelapses .timelapse').count(), 9);
    await page.click('#libraryPager [data-page="1"]');
    assert.equal(await page.locator('#timelapses .timelapse').count(), 6);
    await page.selectOption('#libraryYear', '2024');
    await page.selectOption('#libraryMonth', '07');
    assert.equal(await page.locator('#timelapses .timelapse').count(), 2, 'cross-year clip must match July');
    await page.selectOption('#libraryCamera', '0');
    assert.equal(await page.locator('#timelapses .timelapse').count(), 1);
    await page.fill('#librarySearch', '20231201');
    assert.equal(await page.locator('#timelapses .timelapse').count(), 1);

    await page.evaluate(() => {
      window.savedDay = document.querySelector('[data-date="20240220"]');
      window.savedCard = document.querySelector('.timelapse');
      document.querySelector('#librarySearch').focus();
      window.savedScroll = window.scrollY;
    });
    model.tasks[0].progress.percent = 30;
    await page.evaluate(() => load(true));
    await page.evaluate(() => load());
    assert(await page.evaluate(() => window.savedDay === document.querySelector('[data-date="20240220"]')));
    assert(await page.evaluate(() => window.savedCard === document.querySelector('.timelapse')));
    assert.equal(await page.locator('#yearSelect').inputValue(), '2024');
    assert.equal(await page.locator('#monthSelect').inputValue(), '02');
    assert.equal(await page.locator('#libraryMonth').inputValue(), '07');
    assert.match(await page.locator('#taskPager').innerText(), /2 \/ 2/);
    assert.match(await page.locator('#planPager').innerText(), /2 \/ 2/);
    assert.match(await page.locator('#eventPager').innerText(), /2 \/ 3/);
    assert(await page.locator('#librarySearch').evaluate(node => node === document.activeElement));
    assert.equal(await page.evaluate(() => window.scrollY), await page.evaluate(() => window.savedScroll));

    await page.click('#timelapses [data-watch]');
    await page.evaluate(() => {window.savedVideo = document.querySelector('#playerVideo'); window.savedSrc = window.savedVideo.src;});
    model.plans[0].percent = 56;
    await page.evaluate(() => load());
    assert(await page.locator('#player').evaluate(node => node.open));
    assert(await page.evaluate(() => window.savedVideo === document.querySelector('#playerVideo')));
    assert.equal(await page.locator('#playerVideo').evaluate(node => node.src), await page.evaluate(() => window.savedSrc));
    await page.click('#closePlayer');

    const screenshotRoot = process.env.SCREENSHOT_DIR || os.tmpdir();
    await page.selectOption('#libraryYear', '');
    await page.selectOption('#libraryCamera', '');
    await page.fill('#librarySearch', '');
    await page.selectOption('#monthSelect', '');
    await page.click('#planPager [data-page="0"]');
    await page.evaluate(() => window.scrollTo(0, 0));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(screenshotRoot, 'timelapse-console-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(screenshotRoot, 'timelapse-console-mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('PASS: annual/month calendar, leap year, range selection, task creation, pagination, cross-year filters, stable refresh, player isolation, desktop/mobile layout.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
