/**
 * WWEC Website Backend Bridge - Google Apps Script Web App
 * Handles:
 *  1. Customer Enquiries -> Stored in 'Enquiries' sheet
 *  2. Gallery Projects   -> Metadata stored in 'Gallery' sheet, Media stored in Google Drive folder
 *
 * SECURITY:
 *  - Every request must pass a matching `secret` token matching Script Property `API_SECRET`.
 *  - Uploaded media is placed in the dedicated WWEC Gallery Drive folder and set to
 *    "Anyone with the link can view" so public visitors can view them without private credentials.
 */

const SHEET_ENQUIRIES = 'Enquiries';
const SHEET_GALLERY = 'Gallery';

function getConfig_() {
  const props = PropertiesService.getScriptProperties();
  const secret = props.getProperty('API_SECRET');
  const folderId = props.getProperty('DRIVE_FOLDER_ID');
  if (!secret) {
    throw new Error('Missing Script Property: API_SECRET');
  }
  return { secret: secret, folderId: folderId || '' };
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function ensureSheets_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  let enqSheet = ss.getSheetByName(SHEET_ENQUIRIES);
  if (!enqSheet) {
    enqSheet = ss.insertSheet(SHEET_ENQUIRIES);
    enqSheet.appendRow([
      'ID', 'Submitted At', 'Name', 'Email', 'Phone', 'Subject', 'Message', 'Status'
    ]);
    enqSheet.getRange(1, 1, 1, 8).setFontWeight('bold');
    enqSheet.setFrozenRows(1);
  }

  let galSheet = ss.getSheetByName(SHEET_GALLERY);
  if (!galSheet) {
    galSheet = ss.insertSheet(SHEET_GALLERY);
    galSheet.appendRow([
      'ID', 'Created At', 'Title', 'Category', 'Description', 'Images JSON', 'Status'
    ]);
    galSheet.getRange(1, 1, 1, 7).setFontWeight('bold');
    galSheet.setFrozenRows(1);
  }

  return { ss: ss, enqSheet: enqSheet, galSheet: galSheet };
}

function saveBase64ToDrive_(folder, fileObj, prefixName) {
  const bytes = Utilities.base64Decode(fileObj.base64);
  const safeName = prefixName.replace(/[^a-zA-Z0-9_-]/g, '_') + '_' + fileObj.filename;
  const blob = Utilities.newBlob(bytes, fileObj.mime_type, safeName);
  const driveFile = folder.createFile(blob);
  driveFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  const fileId = driveFile.getId();
  return {
    file_id: fileId,
    name: safeName,
    url: 'https://lh3.googleusercontent.com/d/' + fileId + '=w1600',
    view_url: 'https://drive.google.com/file/d/' + fileId + '/view',
    fallback_url: 'https://drive.google.com/uc?export=view&id=' + fileId
  };
}

function doGet(e) {
  try {
    const cfg = getConfig_();
    const params = (e && e.parameter) || {};
    if (params.secret !== cfg.secret) {
      return jsonResponse_({ ok: false, error: 'Unauthorized request.' });
    }

    const action = params.action || '';
    const { enqSheet, galSheet } = ensureSheets_();

    if (action === 'ping') {
      return jsonResponse_({
        ok: true,
        message: 'WWEC Google Apps Script bridge connected.',
        hasFolder: Boolean(cfg.folderId)
      });
    }

    if (action === 'get_gallery') {
      const items = readGallery_(galSheet);
      return jsonResponse_({ ok: true, items: items });
    }

    if (action === 'get_enquiries') {
      const items = readEnquiries_(enqSheet);
      return jsonResponse_({ ok: true, items: items });
    }

    return jsonResponse_({ ok: false, error: 'Unknown GET action.' });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err.message || err) });
  }
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const cfg = getConfig_();
    const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    const data = JSON.parse(raw);

    if (data.secret !== cfg.secret) {
      return jsonResponse_({ ok: false, error: 'Unauthorized request.' });
    }

    const action = data.action || '';
    const { enqSheet, galSheet } = ensureSheets_();

    // ---- ENQUIRIES ----
    if (action === 'add_enquiry') {
      const id = 'ENQ-' + Utilities.getUuid().slice(0, 8).toUpperCase();
      const submittedAt = data.submitted_at || new Date().toISOString();
      const name = String(data.name || '').trim();
      const email = String(data.email || '').trim();
      const phone = String(data.phone || '').trim();
      const subject = String(data.subject || '').trim();
      const message = String(data.message || '').trim();
      const status = 'New';

      const lastRow = enqSheet.getLastRow();
      if (lastRow > 1) {
        const startRow = Math.max(2, lastRow - 29);
        const recent = enqSheet.getRange(startRow, 1, lastRow - startRow + 1, 8).getValues();
        for (let i = 0; i < recent.length; i++) {
          if (
            String(recent[i][3]).toLowerCase() === email.toLowerCase() &&
            String(recent[i][6]).trim() === message
          ) {
            return jsonResponse_({
              ok: true,
              duplicate: true,
              id: recent[i][0],
              message: 'Duplicate enquiry ignored.'
            });
          }
        }
      }

      enqSheet.appendRow([id, submittedAt, name, email, phone, subject, message, status]);
      return jsonResponse_({ ok: true, id: id });
    }

    if (action === 'update_enquiry_status') {
      const targetId = String(data.id || '').trim();
      const newStatus = String(data.status || '').trim();
      const rowIdx = findRowById_(enqSheet, targetId);
      if (rowIdx === -1) {
        return jsonResponse_({ ok: false, error: 'Enquiry not found.' });
      }
      enqSheet.getRange(rowIdx, 8).setValue(newStatus);
      return jsonResponse_({ ok: true });
    }

    if (action === 'delete_enquiry') {
      const targetId = String(data.id || '').trim();
      const rowIdx = findRowById_(enqSheet, targetId);
      if (rowIdx === -1) {
        return jsonResponse_({ ok: false, error: 'Enquiry not found.' });
      }
      enqSheet.deleteRow(rowIdx);
      return jsonResponse_({ ok: true });
    }

    // ---- GALLERY ----
    if (action === 'add_project') {
      const id = 'PRJ-' + Utilities.getUuid().slice(0, 8).toUpperCase();
      const createdAt = data.created_at || new Date().toISOString();
      const title = String(data.title || '').trim();
      const category = String(data.category || 'completed').trim();
      const description = String(data.description || '').trim();
      const files = Array.isArray(data.files) ? data.files : [];
      const videoFiles = Array.isArray(data.video_files) ? data.video_files : [];
      const externalVideos = Array.isArray(data.external_videos) ? data.external_videos : [];
      const baPairs = Array.isArray(data.before_after_pairs) ? data.before_after_pairs : [];

      const needsDrive = files.length > 0 || videoFiles.length > 0 || baPairs.length > 0;
      let folder = null;
      if (needsDrive) {
        if (!cfg.folderId) {
          return jsonResponse_({ ok: false, error: 'DRIVE_FOLDER_ID is not configured in Script Properties.' });
        }
        folder = DriveApp.getFolderById(cfg.folderId);
      }

      const uploadedPhotos = [];
      for (let i = 0; i < files.length; i++) {
        const saved = saveBase64ToDrive_(folder, files[i], (title || 'WWEC-Project') + '_' + (i + 1));
        uploadedPhotos.push(saved);
      }

      const videosList = [];
      for (let i = 0; i < videoFiles.length; i++) {
        const vf = videoFiles[i];
        const savedVid = saveBase64ToDrive_(folder, vf, (title || 'WWEC-Video') + '_' + (i + 1));
        videosList.push({
          file_id: savedVid.file_id,
          url: 'https://drive.google.com/uc?export=download&id=' + savedVid.file_id,
          embed_url: 'https://drive.google.com/file/d/' + savedVid.file_id + '/preview',
          caption: vf.caption || vf.filename || 'Project Video'
        });
      }
      for (let i = 0; i < externalVideos.length; i++) {
        const ev = externalVideos[i];
        if (ev && ev.url) {
          videosList.push({
            url: ev.url,
            embed_url: ev.embed_url || '',
            caption: ev.caption || 'Project Video'
          });
        }
      }

      const beforeAfterList = [];
      for (let i = 0; i < baPairs.length; i++) {
        const pair = baPairs[i];
        if (!pair || !pair.before || !pair.after) continue;
        const bSaved = saveBase64ToDrive_(folder, pair.before, (title || 'WWEC-BA') + '_before_' + (i + 1));
        const aSaved = saveBase64ToDrive_(folder, pair.after, (title || 'WWEC-BA') + '_after_' + (i + 1));
        beforeAfterList.push({
          before_file_id: bSaved.file_id,
          after_file_id: aSaved.file_id,
          before_url: bSaved.url,
          after_url: aSaved.url,
          caption: pair.caption || ''
        });
      }

      const mediaBundle = {
        photos: uploadedPhotos,
        videos: videosList,
        before_after: beforeAfterList
      };

      galSheet.appendRow([
        id,
        createdAt,
        title,
        category,
        description,
        JSON.stringify(mediaBundle),
        'active'
      ]);

      return jsonResponse_({
        ok: true,
        project: {
          id: id,
          created_at: createdAt,
          title: title,
          category: category,
          description: description,
          images: uploadedPhotos,
          photos: uploadedPhotos,
          videos: videosList,
          before_after: beforeAfterList
        }
      });
    }

    if (action === 'update_project') {
      const targetId = String(data.id || '').trim();
      const rowIdx = findRowById_(galSheet, targetId);
      if (rowIdx === -1) {
        return jsonResponse_({ ok: false, error: 'Project not found.' });
      }

      const title = String(data.title || '').trim();
      const category = String(data.category || 'completed').trim();
      const description = String(data.description || '').trim();

      const existingJson = galSheet.getRange(rowIdx, 6).getValue();
      const parsed = parseMediaCell_(existingJson);

      let photos = Array.isArray(data.kept_photos) ? data.kept_photos : parsed.photos;
      let videos = Array.isArray(data.kept_videos) ? data.kept_videos : parsed.videos;
      let beforeAfter = Array.isArray(data.kept_before_after) ? data.kept_before_after : parsed.before_after;

      // Support legacy remove_file_ids if called directly
      const legacyRemoveIds = Array.isArray(data.remove_file_ids) ? data.remove_file_ids : [];
      if (legacyRemoveIds.length) {
        photos = photos.filter(function(p) {
          if (legacyRemoveIds.indexOf(p.file_id) !== -1) {
            try { DriveApp.getFileById(p.file_id).setTrashed(true); } catch (e) {}
            return false;
          }
          return true;
        });
      }

      // Trash any removed Drive files
      const trashedIds = Array.isArray(data.trashed_file_ids) ? data.trashed_file_ids : [];
      for (let i = 0; i < trashedIds.length; i++) {
        if (trashedIds[i]) {
          try { DriveApp.getFileById(trashedIds[i]).setTrashed(true); } catch (e) {}
        }
      }

      const newFiles = Array.isArray(data.files) ? data.files : [];
      const newVideoFiles = Array.isArray(data.video_files) ? data.video_files : [];
      const newExternalVideos = Array.isArray(data.external_videos) ? data.external_videos : [];
      const newBaPairs = Array.isArray(data.before_after_pairs) ? data.before_after_pairs : [];

      const needsDrive = newFiles.length > 0 || newVideoFiles.length > 0 || newBaPairs.length > 0;
      let folder = null;
      if (needsDrive) {
        if (!cfg.folderId) {
          return jsonResponse_({ ok: false, error: 'DRIVE_FOLDER_ID is not configured.' });
        }
        folder = DriveApp.getFolderById(cfg.folderId);
      }

      for (let i = 0; i < newFiles.length; i++) {
        const saved = saveBase64ToDrive_(folder, newFiles[i], (title || 'WWEC-Project') + '_' + Date.now() + '_' + i);
        photos.push(saved);
      }

      for (let i = 0; i < newVideoFiles.length; i++) {
        const vf = newVideoFiles[i];
        const savedVid = saveBase64ToDrive_(folder, vf, (title || 'WWEC-Video') + '_' + Date.now() + '_' + i);
        videos.push({
          file_id: savedVid.file_id,
          url: 'https://drive.google.com/uc?export=download&id=' + savedVid.file_id,
          embed_url: 'https://drive.google.com/file/d/' + savedVid.file_id + '/preview',
          caption: vf.caption || vf.filename || 'Project Video'
        });
      }

      for (let i = 0; i < newExternalVideos.length; i++) {
        const ev = newExternalVideos[i];
        if (ev && ev.url) {
          videos.push({
            url: ev.url,
            embed_url: ev.embed_url || '',
            caption: ev.caption || 'Project Video'
          });
        }
      }

      for (let i = 0; i < newBaPairs.length; i++) {
        const pair = newBaPairs[i];
        if (!pair || !pair.before || !pair.after) continue;
        const bSaved = saveBase64ToDrive_(folder, pair.before, (title || 'WWEC-BA') + '_before_' + Date.now() + '_' + i);
        const aSaved = saveBase64ToDrive_(folder, pair.after, (title || 'WWEC-BA') + '_after_' + Date.now() + '_' + i);
        beforeAfter.push({
          before_file_id: bSaved.file_id,
          after_file_id: aSaved.file_id,
          before_url: bSaved.url,
          after_url: aSaved.url,
          caption: pair.caption || ''
        });
      }

      galSheet.getRange(rowIdx, 3).setValue(title);
      galSheet.getRange(rowIdx, 4).setValue(category);
      galSheet.getRange(rowIdx, 5).setValue(description);
      galSheet.getRange(rowIdx, 6).setValue(JSON.stringify({
        photos: photos,
        videos: videos,
        before_after: beforeAfter
      }));

      return jsonResponse_({ ok: true });
    }

    if (action === 'delete_project') {
      const targetId = String(data.id || '').trim();
      const rowIdx = findRowById_(galSheet, targetId);
      if (rowIdx === -1) {
        return jsonResponse_({ ok: false, error: 'Project not found.' });
      }

      const existingJson = galSheet.getRange(rowIdx, 6).getValue();
      const parsed = parseMediaCell_(existingJson);
      const allFileIds = [];
      parsed.photos.forEach(function(p) { if (p.file_id) allFileIds.push(p.file_id); });
      parsed.videos.forEach(function(v) { if (v.file_id) allFileIds.push(v.file_id); });
      parsed.before_after.forEach(function(ba) {
        if (ba.before_file_id) allFileIds.push(ba.before_file_id);
        if (ba.after_file_id) allFileIds.push(ba.after_file_id);
      });

      for (let i = 0; i < allFileIds.length; i++) {
        try { DriveApp.getFileById(allFileIds[i]).setTrashed(true); } catch (e) {}
      }

      galSheet.deleteRow(rowIdx);
      return jsonResponse_({ ok: true });
    }

    return jsonResponse_({ ok: false, error: 'Unknown POST action.' });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

function parseMediaCell_(rawVal) {
  let obj = null;
  try {
    obj = JSON.parse(rawVal || '[]');
  } catch (e) {
    obj = [];
  }
  if (Array.isArray(obj)) {
    return { photos: obj, videos: [], before_after: [] };
  }
  if (obj && typeof obj === 'object') {
    return {
      photos: Array.isArray(obj.photos) ? obj.photos : [],
      videos: Array.isArray(obj.videos) ? obj.videos : [],
      before_after: Array.isArray(obj.before_after) ? obj.before_after : []
    };
  }
  return { photos: [], videos: [], before_after: [] };
}

function findRowById_(sheet, targetId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return -1;
  const ids = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (let i = 0; i < ids.length; i++) {
    if (String(ids[i][0]).trim() === targetId) {
      return i + 2;
    }
  }
  return -1;
}

function readEnquiries_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const rows = sheet.getRange(2, 1, lastRow - 1, 8).getValues();
  const out = [];
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    if (!r[0]) continue;
    out.push({
      id: String(r[0]),
      submitted_at: String(r[1]),
      name: String(r[2]),
      email: String(r[3]),
      phone: String(r[4]),
      subject: String(r[5]),
      message: String(r[6]),
      status: String(r[7] || 'New')
    });
  }
  return out;
}

function readGallery_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const rows = sheet.getRange(2, 1, lastRow - 1, 7).getValues();
  const out = [];
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    if (!r[0] || String(r[6]).toLowerCase() === 'deleted') continue;
    const media = parseMediaCell_(r[5]);
    out.push({
      id: String(r[0]),
      created_at: String(r[1]),
      title: String(r[2]),
      category: String(r[3]),
      description: String(r[4]),
      images: media.photos,
      photos: media.photos,
      videos: media.videos,
      before_after: media.before_after
    });
  }
  return out;
}