interface TemplateApiError {
  response?: {
    status?: number;
    data?: {
      detail?:
        | string
        | { message?: string }
        | Array<{ loc?: Array<string | number>; msg?: string }>;
      error?: { message?: string };
    };
  };
  message?: string;
}

export function templateErrorMessage(error: unknown, fallback: string): string {
  const candidate = error as TemplateApiError;
  if (candidate.response?.status === 409) {
    return '模板已被其他会话修改，请刷新模板后重试。';
  }
  if (candidate.response?.status === 401) {
    return '登录状态已失效，请重新登录。';
  }
  const detail = candidate.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const validationMessages = detail
      .map((item) => {
        if (!item.msg) return null;
        const field = item.loc?.filter((part) => part !== 'body').join('.');
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean);
    if (validationMessages.length > 0) return validationMessages.join('；');
  }
  return (
    candidate.response?.data?.error?.message ||
    (!Array.isArray(detail) ? detail?.message : undefined) ||
    candidate.message ||
    fallback
  );
}
