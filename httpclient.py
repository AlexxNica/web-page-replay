#!/usr/bin/env python
# Copyright 2011 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Retrieve web resources over http."""

import httparchive
import httplib
import logging


class DetailedHTTPResponse(httplib.HTTPResponse):
  """Preserve details relevant to replaying responses.

  WARNING: This code uses attributes and methods of HTTPResponse
  that are not part of the public interface.
  """

  def read_chunks(self):
    """Return an array of data.

    The returned chunked have the chunk size and CRLFs stripped off.
    If the response was compressed, the returned data is still compressed.

    Returns:
      [response_body]  # non-chunked responses
      [response_body_chunk_1, response_body_chunk_2, ...]  # chunked responses
    """
    buf = []
    if not self.chunked:
      chunks = [self.read()]
    else:
      try:
        chunks = []
        while True:
          line = self.fp.readline()
          chunk_size = self._read_chunk_size(line)
          if chunk_size is None:
            raise httplib.IncompleteRead(''.join(chunks))
          if chunk_size == 0:
            break
          chunks.append(self._safe_read(chunk_size))
          self._safe_read(2)  # skip the CRLF at the end of the chunk

        # Ignore any trailers.
        while True:
          line = self.fp.readline()
          if not line or line == '\r\n':
            break
      finally:
        self.close()
    return chunks

  @classmethod
  def _read_chunk_size(cls, line):
    chunk_extensions_pos = line.find(';')
    if chunk_extensions_pos != -1:
      line = line[:extention_pos]  # strip chunk-extensions
    try:
      chunk_size = int(line, 16)
    except ValueError:
      return None
    return chunk_size


class DetailedHTTPConnection(httplib.HTTPConnection):
  """Preserve details relevant to replaying connections."""
  response_class = DetailedHTTPResponse


class RealHttpFetch(object):
  def __init__(self, real_dns_lookup):
    self._real_dns_lookup = real_dns_lookup

  def __call__(self, request, headers):
    """Fetch an HTTP request and return the response and response_body.

    Args:
      request: an instance of an ArchivedHttpRequest
      headers: a dict of HTTP headers
    Returns:
      (instance of httplib.HTTPResponse,
       [response_body_chunk_1, response_body_chunk_2, ...])
      # If the response did not use chunked encoding, there is only one chunk.
    """
    logging.debug('RealHttpRequest: %s %s', request.host, request.path)
    host_ip = self._real_dns_lookup(request.host)
    if not host_ip:
      logging.critical('Unable to find host ip for name: %s', request.host)
      return None, None
    try:
      connection = DetailedHTTPConnection(host_ip)
      connection.request(
          request.command,
          request.path,
          request.request_body,
          headers)
      response = connection.getresponse()
      chunks = response.read_chunks()
      return response, chunks
    except Exception, e:
      logging.critical('Could not fetch %s: %s', request, e)
      import traceback
      logging.critical(traceback.format_exc())
      return None, None


class RecordHttpArchiveFetch(object):
  """Make real HTTP fetches and save responses in the given HttpArchive."""

  def __init__(self, http_archive, real_dns_lookup, use_deterministic_script,
               cache_misses=None):
    """Initialize RecordHttpArchiveFetch.

    Args:
      http_archve: an instance of a HttpArchive
      real_dns_lookup: a function that resolves a host to an IP.
      use_deterministic_script: If True, attempt to inject a script,
        when appropriate, to make JavaScript more deterministic.
      cache_misses: instance of CacheMissArchive
    """
    self.http_archive = http_archive
    self.real_http_fetch = RealHttpFetch(real_dns_lookup)
    self.use_deterministic_script = use_deterministic_script
    self.cache_misses = cache_misses
    self.previous_request = None

  def __call__(self, request, request_headers):
    """Fetch the request and return the response.

    Args:
      request: an instance of an ArchivedHttpRequest.
      request_headers: a dict of HTTP headers.
    """

    if self.cache_misses:
      self.cache_misses.record_request(
          request, is_record_mode=True, is_cache_miss=False)

    # if request has already been archived, return the archived version
    if request in self.http_archive:
      logging.debug('Repeated request found: %s\nPrevious Request was: %s\n',
                    request.verbose(),
                    self.previous_request.verbose() if self.previous_request
                    else 'None')
      return self.http_archive[request]

    previous_request = request
    response, response_chunks = self.real_http_fetch(request, request_headers)
    if response is None:
      return None
    archived_http_response = httparchive.ArchivedHttpResponse(
        response.version,
        response.status,
        response.reason,
        response.getheaders(),
        response_chunks)
    if self.use_deterministic_script:
      try:
        archived_http_response.inject_deterministic_script()
      except httparchive.InjectionFailedException as err:
        logging.error('Failed to inject deterministic script for %s', request)
        logging.debug('Request content: %s', err.text)
    logging.debug('Recorded: %s', request)
    self.http_archive[request] = archived_http_response
    return archived_http_response


class ReplayHttpArchiveFetch(object):
  """Serve responses from the given HttpArchive."""

  def __init__(self, http_archive, use_diff_on_unknown_requests=False,
               cache_misses=None, use_closest_match=False):
    """Initialize ReplayHttpArchiveFetch.

    Args:
      http_archive: an instance of a HttpArchive
      use_diff_on_unknown_requests: If True, log unknown requests
        with a diff to requests that look similar.
      cache_misses: Instance of CacheMissArchive.
        Callback updates archive on cache misses
      use_closest_match: If True, on replay mode, serve the closest match
        in the archive instead of giving a 404.
    """
    self.http_archive = http_archive
    self.use_diff_on_unknown_requests = use_diff_on_unknown_requests
    self.cache_misses = cache_misses
    self.use_closest_match = use_closest_match

  def __call__(self, request, request_headers):
    """Fetch the request and return the response.

    Args:
      request: an instance of an ArchivedHttpRequest.
      request_headers: a dict of HTTP headers.
    Returns:
      Instance of ArchivedHttpResponse (if found) or None
    """
    response = self.http_archive.get(request)

    if self.use_closest_match and not response:
      closest_request = self.http_archive.find_closest_request(
          request, use_path=True)
      if closest_request:
        response = self.http_archive.get(closest_request)
        if response:
          logging.info('Request not found: %s\nUsing closest match: %s',
                       request, closest_request)

    if self.cache_misses:
      self.cache_misses.record_request(
          request, is_record_mode=False, is_cache_miss=not response)

    if not response:
      reason = str(request)
      if self.use_diff_on_unknown_requests:
        diff = self.http_archive.diff(request)
        if diff:
          reason += (
              "\nNearest request diff "
              "('-' for archived request, '+' for current request):\n%s" % diff)
      logging.warning('Could not replay: %s', reason)
    return response


class ControllableHttpArchiveFetch(object):
  """Controllable fetch function that can swap between record and replay."""

  def __init__(self, http_archive, real_dns_lookup,
               use_deterministic_script, use_diff_on_unknown_requests,
               use_record_mode, cache_misses, use_closest_match):
    """Initialize HttpArchiveFetch.

    Args:
      http_archive: an instance of a HttpArchive
      real_dns_lookup: a function that resolves a host to an IP.
      use_deterministic_script: If True, attempt to inject a script,
        when appropriate, to make JavaScript more deterministic.
      use_diff_on_unknown_requests: If True, log unknown requests
        with a diff to requests that look similar.
      use_record_mode: If True, start in server in record mode.
      cache_misses: Instance of CacheMissArchive.
      use_closest_match: If True, on replay mode, serve the closest match
        in the archive instead of giving a 404.
    """
    self.record_fetch = RecordHttpArchiveFetch(
        http_archive, real_dns_lookup, use_deterministic_script,
        cache_misses)
    self.replay_fetch = ReplayHttpArchiveFetch(
        http_archive, use_diff_on_unknown_requests, cache_misses,
        use_closest_match)
    if use_record_mode:
      self.SetRecordMode()
    else:
      self.SetReplayMode()

  def SetRecordMode(self):
    self.fetch = self.record_fetch

  def SetReplayMode(self):
    self.fetch = self.replay_fetch

  def __call__(self, *args, **kwargs):
    """Forward calls to Replay/Record fetch functions depending on mode."""
    return self.fetch(*args, **kwargs)
