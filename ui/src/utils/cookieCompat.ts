import {
  parseCookie,
  stringifySetCookie,
  type Cookies,
  type ParseOptions,
  type SerializeOptions,
} from 'cookie';

export function parse(str: string, options?: ParseOptions): Cookies {
  return parseCookie(str, options);
}

export function serialize(name: string, value: string, options?: SerializeOptions): string {
  return stringifySetCookie({ name, value, ...options });
}
