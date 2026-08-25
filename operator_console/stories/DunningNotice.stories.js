import { renderDunningNotice } from "../src/dunning_notice.js";
import firstNoticeMorning from "../fixtures/first_notice_morning.json";
import overdueNoticeEvening from "../fixtures/overdue_notice_evening.json";

export default {
  title: "DunningNotice",
};

export const FirstNoticeMorning = {
  render: () => renderDunningNotice(firstNoticeMorning),
};

export const OverdueNoticeEvening = {
  render: () => renderDunningNotice(overdueNoticeEvening),
};
