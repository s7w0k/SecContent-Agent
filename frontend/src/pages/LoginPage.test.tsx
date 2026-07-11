import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from './LoginPage';

const auth = vi.hoisted(() => ({ login: vi.fn(), register: vi.fn() }));

vi.mock('../auth/useAuth', () => ({
  useAuth: () => auth,
}));

function fill(name: string, value: string) {
  fireEvent.change(screen.getByLabelText(name), { target: { value } });
}

function submitForm() {
  const button = document.querySelector<HTMLButtonElement>('button[type="submit"]');
  if (!button) throw new Error('Submit button not found');
  fireEvent.click(button);
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('validates username and password before login', async () => {
    render(<LoginPage />);
    fill('用户名', 'a');
    fill('密码', '123');

    submitForm();

    expect(await screen.findByText('请输入 3-20 位字母、数字或下划线')).toBeInTheDocument();
    expect(screen.getByText('密码长度须为 6-32 位')).toBeInTheDocument();
    expect(auth.login).not.toHaveBeenCalled();
  });

  it('submits valid login credentials', async () => {
    auth.login.mockResolvedValue(undefined);
    render(<LoginPage />);
    fill('用户名', 'alice');
    fill('密码', 'secret1');

    submitForm();

    await waitFor(() =>
      expect(auth.login).toHaveBeenCalledWith({ username: 'alice', password: 'secret1' }),
    );
  });

  it('switches to registration and submits the extended form', async () => {
    auth.register.mockResolvedValue(undefined);
    render(<LoginPage />);
    fireEvent.click(screen.getByText('注册'));

    fill('用户名', 'alice');
    fill('显示名称', 'Alice Zhang');
    fill('邮箱', 'alice@example.com');
    fill('密码', 'secret1');
    fill('确认密码', 'secret1');
    submitForm();

    await waitFor(() =>
      expect(auth.register).toHaveBeenCalledWith({
        username: 'alice',
        password: 'secret1',
        display_name: 'Alice Zhang',
        email: 'alice@example.com',
      }),
    );
  });

  it('shows the backend error message when login fails', async () => {
    auth.login.mockRejectedValue({ response: { data: { detail: '用户名或密码错误' } } });
    render(<LoginPage />);
    fill('用户名', 'alice');
    fill('密码', 'wrong12');

    submitForm();

    expect(await screen.findByText('用户名或密码错误')).toBeInTheDocument();
  });
});
