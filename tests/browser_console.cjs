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
  duration_adjusted_files: index === 1 ? 2 : 0,
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
  scan_percent: index === 0 ? 55.6 : 100, export_percent: index === 0 ? 0 : 100,
  current_date: '20240229', completed_days: index === 0 ? 50 : 90, total_days: 90,
  partial_days: 1, failed_days: 0, retry_ids: [], outputs: index ? [timelapses[0].name] : [],
}));
let model = {dates, timelapses, tasks, plans, queue_paused: false, queued_count: 9,
  diagnostics: {available: true, recognized_files: 10000, mp4_files: 10000}};
let created = [];
let savedDirectories = [], refreshed = 0;
const directories = {input_root: '/input', export_root: '/output', state_root: '/output',
  scan_roots: [{path: '/input', available: true}, {path: '/recordings', available: false}],
  export_roots: [{path: '/output', available: true}, {path: '/exports', available: false}],
  diagnostics: {available: true, mp4_files: 1200, recognized_files: 1198, scanned_at: '2000-01-01T12:00:00Z'}};

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      let body = model;
      if (pathname === '/api/directories') {
        if(request.method() === 'POST')savedDirectories.push(request.postDataJSON());
        body = directories;
      } else if (pathname === '/api/directories/browse') {
        const current = new URL(request.url()).searchParams.get('path');
        body = {path: current, children: current === '/input' ? [{name:'archive', path:'/input/archive'}] : []};
      } else if (pathname === '/api/index') {
        refreshed++; body = {indexing: true};
      } else if (pathname.startsWith('/api/date/')) {
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

    model.tasks[0] = {...model.tasks[0], kind: 'export', camera: 'legacy', dates: ['20240101', '20241231'],
      progress: {phase: 'exporting', percent: 12, current_file: 'generated-history.mp4',
        file_index: 12, total_files: 100, frames: 250, estimated_frames: 2000, reused_files: 8,
        frames_per_second: 1.2, estimated_output_seconds: 80, duration_adjusted_files: 3,
        failed_files: 0, eta_seconds: 600, heartbeat_at: Date.now()/1000}};
    model.plans[0] = {...model.plans[0], phase: 'exporting', percent: 98,
      completed_days: 90, scan_percent: 100, export_percent: 12};
    await page.evaluate(() => load(true));
    await page.click('#planPager [data-page="0"]');
    const firstPlan = page.locator('#plans .plan').first();
    assert.match(await firstPlan.innerText(), /人物扫描 · 90\/90 天\s+100%/);
    assert.match(await firstPlan.innerText(), /视频合成\s+12%/);
    assert.doesNotMatch(await firstPlan.innerText(), /98%/);
    assert.match(await page.locator('#missionTitle').innerText(), /2024.01.01 — 2024.12.31/);
    assert.match(await page.locator('#missionText').innerText(), /复用 8 个片段 · 解码 1.2 帧\/秒/);
    assert.match(await page.locator('#missionText').innerText(), /时长校正 3 个/);
    model.tasks[0].progress.phase = 'encoding';
    await page.evaluate(() => load(true));
    assert.match(await page.locator('#missionTitle').innerText(), /正在拼接已保存片段/);
    model.tasks[0].progress.phase = 'exporting';
    await page.evaluate(() => load(true));

    const screenshotRoot = process.env.SCREENSHOT_DIR || os.tmpdir();
    await page.selectOption('#libraryYear', '');
    await page.selectOption('#libraryCamera', '');
    await page.fill('#librarySearch', '');
    await page.selectOption('#monthSelect', '');
    await page.evaluate(() => window.scrollTo(0, 0));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(screenshotRoot, 'timelapse-console-desktop.png'), fullPage: true});
    await page.screenshot({path: path.join(screenshotRoot, 'timelapse-console-progress.png')});
    await page.click('#openDirectories');
    await page.waitForFunction(() => document.querySelector('#scanDirectory').value === '/input');
    assert(await page.locator('[data-directory-root="/recordings"]').isDisabled());
    await page.click('[data-browse-kind="scan"]');
    await page.click('[data-directory-root="/input/archive"]');
    await page.waitForFunction(() => document.querySelector('#scanDirectory').value === '/input/archive');
    assert.equal(await page.locator('#scanDirectory').inputValue(), '/input/archive');
    await page.fill('#exportDirectory', '/output/films');
    await page.evaluate(() => load());
    await page.evaluate(() => load(true));
    assert.equal(await page.locator('#scanDirectory').inputValue(), '/input/archive');
    assert.equal(await page.locator('#exportDirectory').inputValue(), '/output/films');
    await page.click('#saveDirectories');
    await page.waitForFunction(() => document.querySelector('#directoryNotice').textContent.includes('已保存'));
    assert.deepEqual(savedDirectories, [{input_root: '/input/archive', export_root:'/output/films'}]);
    await page.click('#directoryRefresh');
    await page.waitForFunction(() => document.querySelector('#directoryNotice').textContent.includes('已请求刷新'));
    assert.equal(refreshed, 1);
    await page.screenshot({path:path.join(screenshotRoot, 'timelapse-directory-desktop.png')});
    await page.click('#closeDirectories');
    await page.setViewportSize({width: 390, height: 844});
    await page.reload();
    await page.waitForSelector('.month');
    assert.equal(await page.locator('header').count(), 1);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.screenshot({path: path.join(screenshotRoot, 'timelapse-console-mobile.png')});
    await page.click('#openDirectories');
    await page.waitForFunction(() => document.querySelector('#scanDirectory').value === '/input');
    assert(await page.locator('#directoryDialog').evaluate(node => node.scrollWidth <= node.clientWidth));
    await page.screenshot({path:path.join(screenshotRoot, 'timelapse-directory-mobile.png')});
    assert.deepEqual(errors, []);
    assert.match(await page.locator('#timelapses').innerText(), /已按实际录像时长校正 2 个素材/);
    console.log('PASS: calendar, range selection, pagination, filters, stable refresh, player isolation, separate stage progress, checkpoint details, desktop/mobile layout.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
