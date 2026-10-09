import type { ThemeConfig } from 'antd';

/**
 * antd 5 Token 主题。
 * 本项目不引入 Tailwind，全部视觉统一由 antd Token 驱动。
 */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#1677ff',
    colorSuccess: '#52c41a',
    colorWarning: '#faad14',
    colorError: '#ff4d4f',
    borderRadius: 6,
    fontSize: 14,
    wireframe: false,
  },
  components: {
    Layout: {
      siderBg: '#001529',
      headerBg: '#ffffff',
      headerHeight: 56,
      bodyBg: '#f5f7fa',
    },
    Table: {
      headerBg: '#fafafa',
      cellPaddingBlock: 12,
    },
    Card: {
      borderRadiusLG: 8,
    },
  },
};

/** Mock 模式提示条配色（全局统一，避免各页面各写一套） */
export const MOCK_BANNER_COLOR = {
  background: '#fff7e6',
  border: '#ffd591',
  text: '#ad6800',
};

/** 红线告警配色（越权 / 禁止上架） */
export const DANGER_BANNER_COLOR = {
  background: '#fff1f0',
  border: '#ffa39e',
  text: '#a8071a',
};
