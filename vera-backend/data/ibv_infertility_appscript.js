const DATA_MAPPING = {
  patient_information: {
    chart_number: ["J8", true],
    patient_name: ["J9", true],
    patient_dob: ["J10", true],
    patient_gender: ["J11", true],
    spouse_partner_name: ["J12", false],
    spouse_partner_dob: ["J13", false],
    spouse_gender: ["J14", false],
  },
  patient_verification: {
    is_insurance_active: ["J26", false],
  },
  insurance_information: {
    doctor_inside_network: ["J16", false],
    facility_inside_network: ["J17", false],
    out_of_network_coverage: ["J18", false],
    plan_type: ["J19", false],
    cob_status: ["J20", false],
    policy_number: ["J21", true],
    group_number: ["J22", false],
    group_name: ["J23", false],
    policy_situs: ["J24", false],
  },
  appointment_information: {
    appointment_type: ["AD8", false],
    appointment_date: ["AD9", true],
  },
  verification_information: {
    verified_by: ["AD12", true],
    verified_at: ["AD13", false],
    callback_number: ["AD14", true],
  },
  benefit_coverage: {
    benefit_year_type: ["AD16", false],
    plan_effective_date: ["AD17", false],
    plan_year_information: ["AD18", false],
    coverage_type: ["AD19", false],
    pcp_referral_required: ["AD20", false],
    telehealth_covered: ["AD21", false],
    plan_fund_type: ["AD22", false],
    employer_support_size: ["AD23", false],
    infertility_plan_mandate: ["AD24", false],
  },
  hospital_information: {
    hospital_name: ["AO2", true],
    hospital_address: ["AR3", true],
    tax_id: ["AR6", true],
    npi: ["AR7", true],
  },
  provider_reference_information: {
    provider_name: ["AR10", true],
    npi: ["AR11", true],
    office_location: ["AR12", true],
  },
  general_coverage: {
    office_visits: {
      cpt_99211: {
        covered: ["M29", false],
        copay: ["P29", false],
        coinsurance: ["S29", false],
        prior_auth: ["V29", false],
      },
    },
    asc_professional: {
      cpt_58555: {
        covered: ["M30", false],
        copay: ["P30", false],
        coinsurance: ["S30", false],
        prior_auth: ["V30", false],
      },
    },
    asc_facility: {
      cpt_58555: {
        covered: ["M31", false],
        copay: ["P31", false],
        coinsurance: ["S31", false],
        prior_auth: ["V31", false],
      },
    },
  },
  diagnostic_testing: {
    diagnostic_testing_covered: ["J33", false],
    labs_xray_ultrasound: {
      cpt_58340: {
        covered: ["M35", false],
        copay: ["P35", false],
        coinsurance: ["S35", false],
        prior_auth: ["V35", false],
      },
      cpt_82670: {
        covered: ["M36", false],
        copay: ["P36", false],
        coinsurance: ["S36", false],
        prior_auth: ["V36", false],
      },
      cpt_83001: {
        covered: ["M37", false],
        copay: ["P37", false],
        coinsurance: ["S37", false],
        prior_auth: ["V37", false],
      },
      cpt_83002: {
        covered: ["M38", false],
        copay: ["P38", false],
        coinsurance: ["S38", false],
        prior_auth: ["V38", false],
      },
      cpt_84146: {
        covered: ["M39", false],
        copay: ["P39", false],
        coinsurance: ["S39", false],
        prior_auth: ["V39", false],
      },
      cpt_84443: {
        covered: ["M40", false],
        copay: ["P40", false],
        coinsurance: ["S40", false],
        prior_auth: ["V40", false],
      },
      cpt_84144: {
        covered: ["M41", false],
        copay: ["P41", false],
        coinsurance: ["S41", false],
        prior_auth: ["V41", false],
      },
      cpt_76830: {
        covered: ["M42", false],
        copay: ["P42", false],
        coinsurance: ["S42", false],
        prior_auth: ["V42", false],
      },
    },
  },
  male_partner_coverage: {
    male_partner_covered: ["J44", false],
    semen_analysis: {
      cpt_89320: {
        covered: ["M46", false],
        copay: ["P46", false],
        coinsurance: ["S46", false],
        prior_auth: ["V46", false],
      },
    },
    sperm_cryopreservation: {
      cpt_89259: {
        covered: ["M47", false],
        copay: ["P47", false],
        coinsurance: ["S47", false],
        prior_auth: ["V47", false],
      },
    },
  },
  infertility_treatment: {
    infertility_tx_covered: ["J49", false],
    ovulation_induction: {
      covered: ["M51", false],
      copay: ["P51", false],
      coinsurance: ["S51", false],
      prior_auth: ["V51", false],
      cycle_limit: ["Y51", false],
      additional_notes: ["AB51", false],
    },
    intrauterine_insemination: {
      cpt_58323: {
        covered: ["M52", false],
        copay: ["P52", false],
        coinsurance: ["S52", false],
        prior_auth: ["V52", false],
      },
      cpt_58322: {
        covered: ["M53", false],
        copay: ["P53", false],
        coinsurance: ["S53", false],
        prior_auth: ["V53", false],
      },
      cpt_89261: {
        covered: ["M54", false],
        copay: ["P54", false],
        coinsurance: ["S54", false],
        prior_auth: ["V54", false],
      },
      cycle_limit: ["Y52", false],
      additional_notes: ["AB52", false],
    },
    in_vitro_fertilization: {
      cpt_58970: {
        covered: ["M55", false],
        copay: ["P55", false],
        coinsurance: ["S55", false],
        prior_auth: ["V55", false],
      },
      cpt_89280: {
        covered: ["M56", false],
        copay: ["P56", false],
        coinsurance: ["S56", false],
        prior_auth: ["V56", false],
      },
      cpt_89253: {
        covered: ["M57", false],
        copay: ["P57", false],
        coinsurance: ["S57", false],
        prior_auth: ["V57", false],
      },
      cycle_limit: ["Y55", false],
      additional_notes: ["AB55", false],
    },
    embryo_cryopreservation: {
      cpt_89258: {
        covered: ["M58", false],
        copay: ["P58", false],
        coinsurance: ["S58", false],
        prior_auth: ["V58", false],
      },
      cpt_89342: {
        covered: ["M59", false],
        copay: ["P59", false],
        coinsurance: ["S59", false],
        prior_auth: ["V59", false],
      },
      cycle_limit: ["Y58", false],
      additional_notes: ["AB58", false],
    },
    egg_cryopreservation_elective: {
      cpt_89337: {
        covered: ["M60", false],
        copay: ["P60", false],
        coinsurance: ["S60", false],
        prior_auth: ["V60", false],
      },
      cycle_limit: ["Y60", false],
      additional_notes: ["AB60", false],
    },
    egg_cryopreservation_cancer: {
      cpt_89337: {
        covered: ["M61", false],
        copay: ["P61", false],
        coinsurance: ["S61", false],
        prior_auth: ["V61", false],
      },
      cycle_limit: ["Y61", false],
      additional_notes: ["AB61", false],
    },
    frozen_embryo_transfer: {
      cpt_58974: {
        covered: ["M62", false],
        copay: ["P62", false],
        coinsurance: ["S62", false],
        prior_auth: ["V62", false],
      },
      cycle_limit: ["Y62", false],
      additional_notes: ["AB62", false],
    },
    embryo_biopsy: {
      cpt_89290: {
        covered: ["M63", false],
        copay: ["P63", false],
        coinsurance: ["S63", false],
        prior_auth: ["V63", false],
      },
      cpt_89291: {
        covered: ["M64", false],
        copay: ["P64", false],
        coinsurance: ["S64", false],
        prior_auth: ["V64", false],
      },
      cycle_limit: ["Y63", false],
      additional_notes: ["AB63", false],
    },
  },
  deductibles: {
    individual: {
      total: ["AD30", false],
      met_amount: ["AD31", false],
      remaining: ["AD32", false],
    },
    family: {
      total: ["AH30", false],
      met_amount: ["AH31", false],
      remaining: ["AH32", false],
    },
  },
  out_of_pocket: {
    individual: {
      total: ["AD33", false],
      met_amount: ["AD34", false],
      remaining: ["AD35", false],
    },
    family: {
      total: ["AH33", false],
      met_amount: ["AH34", false],
      remaining: ["AH35", false],
    },
  },
  lifetime_maximum: {
    total: ["J69", false],
    met_amount: ["J70", false],
    remaining: ["J71", false],
    applicable_area: ["J72", false],
    additional_notes: ["P69", false],
  },
  embryo_cryo_storage: {
    storage_time_coverage: ["J66", false],
  },
  enrollment: {
    enrollment_required: ["AD66", false],
    enrollment_provider_name: ["AD67", false],
    enrollment_provider_phone: ["AD68", false],
    center_of_excellence_required: ["AD69", false],
  },
  authorization_department: {
    auth_department_name: ["J74", false],
    auth_department_phone: ["J75", false],
  },
  third_party_administrator: {
    tpa_exists: ["AD75", false],
    tpa_name: ["AD76", false],
  },
  pharmacy_benefit_manager: {
    pbm_exists: ["J77", false],
    pbm_name: ["J78", false],
    pbm_phone: ["J79", false],
  },
  infertility_specialty_pharmacy: {
    isp_exists: ["AA71", false],
    isp_name: ["AA72", false],
    isp_phone: ["AA73", false],
  },
  insurance_reference_information: {
    insurance_provider_name: ["AR18", true],
    insurance_phone_number: ["AR19", true],
    web_portal: ["AR20", false],
  },
  insurance_representative: {
    rep_name: ["AD78", false],
    call_reference_number: ["AD79", false],
  },
  form_information: {
    practice: ["H84", true],
    ibv_form_type: ["H85", true],
  },
};

function createCellToKeyMap(mapping) {
  const invertedMap = {};

  function traverse(currentObj) {
    for (const key in currentObj) {
      if (!currentObj.hasOwnProperty(key)) continue;

      const value = currentObj[key];

      if (Array.isArray(value)) {
        const cellAddress = value[0].toUpperCase();

        invertedMap[cellAddress] = key;
      } else if (typeof value === "object" && value !== null) {
        // Recursive case: If the value is another object, traverse it.
        traverse(value);
      }
      // Ignore other types (like the boolean false if it was somehow standalone)
    }
  }

  // Start the recursion
  traverse(mapping);

  return invertedMap;
}

// Global variable (or passed around) for quick lookups
const CELL_TO_KEY_LOOKUP = createCellToKeyMap(DATA_MAPPING);

function getFieldKey(cellAddress) {
  // Ensure the incoming address is in uppercase for the lookup
  return CELL_TO_KEY_LOOKUP[cellAddress.toUpperCase()];
}

let missingCells = [];

// function sendDataToExternalSystem() {
//     initializeProperties();
//     const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
//     const sheet = spreadsheet.getActiveSheet();
//     const timeZone = spreadsheet.getSpreadsheetTimeZone();
//     const ui = SpreadsheetApp.getUi();

//     let dataToSend = {};
//     for (const key in DATA_MAPPING) {
//         const dataJson = DATA_MAPPING[key];
//         let extractedData = null;

//         extractedData = readCellFields(sheet, timeZone, dataJson);
//         // Assign the result to the final payload key
//         if (extractedData !== null) {
//           dataToSend[key] = extractedData;
//         }
//       }

//     // CHANGED: Vera-native body (no form_data/sections wrapper; real UUIDs)
//    const finalpayload = {
//   "form_type_id":      "019eef53-9858-77f0-921c-f5da5f50cca6",
//   "schema_version_id": "019eef53-9863-7713-b460-b7dc0bdf2a2e",
//   "intake_payload":    dataToSend
//    };

//     checkRequiredCellsPresent(sheet);

//     const finalJsonString = JSON.stringify(finalpayload);
//     Logger.log("Generated Final JSON Payload:\n" + finalJsonString);

//     const getHost = getEnvWiseHost(sheet);
//     const HOST = PropertiesService.getScriptProperties().getProperty(getHost);

//     if (!HOST) {
//       throw Error ("HOST for this environment is missing!");
//     }

//     // CHANGED: Vera endpoint path
//     const API_ENDPOINT = `https://${HOST}/api/v1/patient-forms`;
//     const getApiKey = getEnvWiseApiKey(sheet);

//     const API_KEY = PropertiesService.getScriptProperties().getProperty(getApiKey);

//     if (!API_KEY) {
//       throw Error ("API Key for this environment is missing!");
//     }
//     Logger.log(" URL " + API_ENDPOINT);

//     const options = {
//       'method': 'post',
//       'payload': finalJsonString,
//       'headers': {
//         'content-Type': 'application/json',
//         'Authorization': 'Bearer ' + API_KEY,
//         'X-Pinggy-No-Screen' : 'test'
//       },
//       'muteHttpExceptions': true
//     };

//     try {
//       const response = UrlFetchApp.fetch(API_ENDPOINT, options);
//       const responseCode = response.getResponseCode();
//       Logger.log("response code " + responseCode);

//       const responseText = response.getContentText();
//       Logger.log("response text " + responseText);

//       if (responseCode === 200) {
//         const result = JSON.parse(responseText);
//         ui.alert('✅ Submission successful to Vera!', "Upload complete", ui.ButtonSet.OK);
//       } else {
//         // FIXED: was referencing an undefined `message` var; show the real response body.
//         ui.alert('❌ ERROR: Submission Failed (' + responseCode + ')', responseText, ui.ButtonSet.OK);
//       }

//     } catch (e) {
//       SpreadsheetApp.getUi().alert(`An internal error occurred: ${e.toString()}`);
//     }
//   }

function getEnvWiseFormTypeId(sheet) {
  const value = sheet.getRange("BB6").getValue();
  if (value === "DEV") return "FORM_TYPE_ID_DEV";
  if (value === "TEST") return "FORM_TYPE_ID_TEST";
  if (value === "LOCAL") return "FORM_TYPE_ID_LOCAL";
  return "";
}

function getEnvWiseSchemaVersionId(sheet) {
  const value = sheet.getRange("BB6").getValue();
  if (value === "DEV") return "SCHEMA_VERSION_ID_DEV";
  if (value === "TEST") return "SCHEMA_VERSION_ID_TEST";
  if (value === "LOCAL") return "SCHEMA_VERSION_ID_LOCAL";
  return "";
}

function sendDataToExternalSystem() {
  initializeProperties();
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = spreadsheet.getActiveSheet();
  const timeZone = spreadsheet.getSpreadsheetTimeZone();
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();

  let dataToSend = {};
  for (const key in DATA_MAPPING) {
    const dataJson = DATA_MAPPING[key];
    let extractedData = null;
    extractedData = readCellFields(sheet, timeZone, dataJson);
    if (extractedData !== null) {
      dataToSend[key] = extractedData;
    }
  }

  // form_type_id + schema_version_id now come from Script Properties (per environment)
  const formTypeId = props.getProperty(getEnvWiseFormTypeId(sheet));
  const schemaVersionId = props.getProperty(getEnvWiseSchemaVersionId(sheet));

  if (!formTypeId || !schemaVersionId) {
    throw Error(
      "form_type_id / schema_version_id for this environment is missing!",
    );
  }

  const finalpayload = {
    form_type_id: formTypeId,
    schema_version_id: schemaVersionId,
    intake_payload: dataToSend,
  };

  checkRequiredCellsPresent(sheet);

  const finalJsonString = JSON.stringify(finalpayload);
  // PHI-safe: log which sections are populated, never the patient values inside them.
  Logger.log(
    "Submitting intake payload — sections: " + Object.keys(dataToSend).join(", "),
  );

  const HOST = props.getProperty(getEnvWiseHost(sheet));
  if (!HOST) {
    throw Error("HOST for this environment is missing!");
  }

  const API_ENDPOINT = `https://${HOST}/api/v1/patient-forms`;
  const API_KEY = props.getProperty(getEnvWiseApiKey(sheet));
  if (!API_KEY) {
    throw Error("API Key for this environment is missing!");
  }
  Logger.log(" URL " + API_ENDPOINT);

  const options = {
    method: "post",
    payload: finalJsonString,
    headers: {
      "content-Type": "application/json",
      Authorization: "Bearer " + API_KEY,
      "X-Pinggy-No-Screen": "test",
    },
    muteHttpExceptions: true,
  };

  try {
    const response = UrlFetchApp.fetch(API_ENDPOINT, options);
    const responseCode = response.getResponseCode();
    Logger.log("response code " + responseCode);

    const responseText = response.getContentText();
    Logger.log("response text " + responseText);

    if (responseCode === 200) {
      const result = JSON.parse(responseText);
      ui.alert(
        "✅ Submission successful to Vera!",
        "Upload complete",
        ui.ButtonSet.OK,
      );
    } else {
      ui.alert(
        "❌ ERROR: Submission Failed (" + responseCode + ")",
        responseText,
        ui.ButtonSet.OK,
      );
    }
  } catch (e) {
    SpreadsheetApp.getUi().alert(`An internal error occurred: ${e.toString()}`);
  }
}

function getEnvWiseApiKey(sheet) {
  const value = sheet.getRange("BB6").getValue();
  if (value === "DEV") {
    return "EXTERNAL_DEV_API_KEY";
  } else if (value === "TEST") {
    return "EXTERNAL_TEST_API_KEY";
  } else if (value === "LOCAL") {
    return "EXTERNAL_LOCAL_API_KEY";
  }
  return "";
}

function getEnvWiseHost(sheet) {
  const value = sheet.getRange("BB6").getValue();
  if (value === "DEV") {
    return "SC_DEV_HOST";
  } else if (value === "TEST") {
    return "SC_TEST_HOST";
  } else if (value === "LOCAL") {
    return "SC_LOCAL_HOST";
  }
  return "";
}

function checkRequiredCellsPresent(sheet) {
  if (missingCells.length > 0) {
    const detailedMissingInfo = missingCells
      .map((missingCell) => {
        const keyCell = getFieldKey(missingCell);
        return `(Cell: ${missingCell} - ${keyCell})`;
      })
      .join(",\n");

    SpreadsheetApp.getUi().alert(
      `🚨 Required fields are missing! Please fill in the following cells:\n\n${detailedMissingInfo}`,
    );

    // Optional: highlight missing cells in red
    missingCells.forEach((cell) => {
      sheet.getRange(cell).setBackground("#f8d7da");
    });

    throw new Error("Missing required cell values — process stopped.");
  }
}

function readCellFields(sheet, timeZone, fieldsMap) {
  const dataObject = {};

  // coverage type based validation
  const cellValue = sheet.getRange("AD19").getValue().toString().trim();
  if (cellValue.toLowerCase() === "family") {
    let spouse_name = sheet.getRange("J12").getValue();
    let spouse_dob = sheet.getRange("J13").getValue();
    let spouse_gender = sheet.getRange("J14").getValue();
    let value = "";
    if (!spouse_name) {
      value = value + "(Cell J12) Spouse Partner Name" + ", ";
    }
    if (!spouse_dob) {
      value = value + "(Cell J13) Spouse Partner DOB " + ", ";
    }
    if (!spouse_gender) {
      value = value + "(Cell J14) Spouse Partner Gender ";
    }
    if (value) {
      SpreadsheetApp.getUi().alert(
        `🚨 Required fields are missing! Please fill in the following cells:\n\n${value}`,
      );
      throw new Error("Missing required cell values — process stopped.");
    }
  }

  for (const jsonKey in fieldsMap) {
    const cellAddress = fieldsMap[jsonKey];
    // const range = sheet.getRange(cellAddress);
    // let value = range.getValue();
    let value = resolveRefs(sheet, cellAddress);
    if (value instanceof Date) {
      value = Utilities.formatDate(value, timeZone, "yyyy-MM-dd");
    }

    value = getFormattedValue(jsonKey, value);

    if (cellAddress[0] !== "AD54") {
      //validate required fields with proper values
      validateCellValues(jsonKey, value);
    }

    //Logger.log('json key, value and type ' + jsonKey + ' & ' + value + ' & ' + typeof value);
    if (value !== "") {
      dataObject[jsonKey] = value;
    }
    //dataObject[jsonKey] = value === "" ? null : value;
  }

  return dataObject;
}

// A central configuration object to map jsonKey to its validator and error message details
const VALIDATION_RULES = {
  patient_name: { validator: isValidProperName, displayName: "Patient name" },
  policy_number: { validator: isValidPolicyId, displayName: "Policy Id" },
  insurance: { validator: isValidProperName, displayName: "Insurance name" },
  phone_number: { validator: isValidPhoneNumber, displayName: "Phone number" },
  pbm_phone_number: {
    validator: isValidPhoneNumber,
    displayName: "Pbm Phone number",
  },
  auth_dept_phone_number: {
    validator: isValidPhoneNumber,
    displayName: "Auth Dept Phone number",
  },
  portal: { validator: isValidURL, displayName: "Portal URL" },
  provider_name: {
    validator: isValidFlexibleName,
    displayName: "Insurance provider name",
  },
  npi: { validator: isValidNpiNumber, displayName: "Npi number" },
  // Add more rules here without touching the function body
};

function validateCellValues(jsonKey, value) {
  if (jsonKey === "pbm_phone_number" && value === "") {
    return;
  }
  if (jsonKey === "auth_dept_phone_number" && value === "") {
    return;
  }
  const rule = VALIDATION_RULES[jsonKey];

  // 1. Check if a rule exists for this key
  if (!rule) {
    // Option 1: Ignore (if no validation is needed for this key)
    return;

    // Option 2: Throw an error (if every key MUST have a rule)
    // throw new Error(`No validation rule defined for key: ${jsonKey}`);
  }

  // 2. Perform validation and throw error if it fails
  // PHI-safe: log the field being validated, never its value.
  Logger.log("Validating field: " + jsonKey);
  if (!rule.validator(value)) {
    SpreadsheetApp.getUi().alert(
      `🚨 ${rule.displayName}: ${value} is not valid. Please put the valid information!`,
    );
    throw new Error(` : Process Stopped!`);
  }
}

// insurance provider name validation
function isValidFlexibleName(cellValue) {
  // 1. Handle null or undefined values immediately
  if (cellValue === null || cellValue === undefined) {
    return true;
  }
  if (typeof cellValue !== "string") {
    return false; // Reject non-strings or empty/whitespace-only strings
  }
  if (cellValue.trim() === "") {
    return true;
  }

  const invalidCharRegex = /[^a-zA-Z\s\.'\-]/;
  if (invalidCharRegex.test(cellValue)) {
    return false;
  }

  const trimmedValue = cellValue.trim();
  if (
    trimmedValue.startsWith("-") ||
    trimmedValue.endsWith("-") ||
    trimmedValue.startsWith(".") ||
    trimmedValue.endsWith(".")
  ) {
    return false;
  }

  // B. Must not be just a title or initial (e.g., "Dr.", "J.")
  if (trimmedValue.length < 2) {
    return false;
  }

  // C. Check for double symbols (e.g., "--", "..", "''") - OPTIONAL
  if (
    trimmedValue.includes("--") ||
    trimmedValue.includes("..") ||
    trimmedValue.includes("''")
  ) {
    // return false;
  }

  if (!/[a-zA-Z]/.test(trimmedValue)) {
    return false; // Must contain at least one letter
  }
  // If all checks pass, it's considered valid under these flexible rules.
  return true;
}

//npi number validation
function isValidNpiNumber(cellValue) {
  if (typeof cellValue !== "string" || cellValue.trim() === "") {
    return false;
  }

  // 2. Regular Expression Check (The core validation)
  // RegEx breakdown:
  // ^        Start of the string
  // \d       A digit (equivalent to [0-9])
  // {10}     Must occur exactly 10 times
  // $        End of the string
  const digitRegex = /^\d{10}$/;

  return digitRegex.test(cellValue);
}

//url validations
function isValidURL(cellValue) {
  if (typeof cellValue !== "string" || cellValue.trim() === "") {
    return false;
  }
  const urlRegex =
    /^(https?:\/\/)?([\da-z\.-]+)\.([a-z\.]{2,6})([\/\w\.-]*)*\/?(\?[\w\=\&\%\+\-\.\/:]*)?(\#[\w\-\/]*)?$/i;

  return urlRegex.test(cellValue.trim());
}

//phone number validation
function isValidPhoneNumber(cellValue) {
  // 1. Basic Type Check
  if (typeof cellValue !== "string" || cellValue.trim() === "") {
    return false;
  }

  const allowedCharsRegex = /^[0-9\s()\-+]+$/;
  if (!allowedCharsRegex.test(cellValue)) {
    return false; // Invalid characters detected
  }

  const digitsOnly = cellValue.replace(/[\s()\-+]/g, "");

  const minLength = 9;
  const maxLength = 13;

  if (digitsOnly.length < minLength || digitsOnly.length > maxLength) {
    return false; // Fails the length requirement
  }

  // All checks passed
  return true;
}

// Name validation
function isValidProperName(cellValue) {
  if (typeof cellValue !== "string" || cellValue.trim() === "") {
    return false; // Reject non-strings or empty/whitespace-only strings
  }

  // Regex breakdown:
  // ^               Start of string
  // [A-Za-z\s'-]+   One or more of: standard letters, space, hyphen, apostrophe
  // $               End of string
  const nameRegex = /^[A-Za-z\s'-]+$/;

  // Additional checks for better "proper name" validation:
  // 1. Must not start or end with a space or symbol
  if (cellValue.trim() !== cellValue || cellValue.length > 50) {
    return false;
  }

  // 2. Must not contain numbers
  if (/\d/.test(cellValue)) {
    return false;
  }

  // Final check against the main structure
  return nameRegex.test(cellValue);
}

// Policy Id validation
function isValidPolicyId(cellValue) {
  // 1. Basic type and emptiness check
  if (typeof cellValue !== "string" || cellValue.trim() === "") {
    return false;
  }

  // The policy ID cannot contain leading or trailing spaces, so we test the original value.
  // The RegEx must start (^) and end ($) with only allowed characters.
  const policyIdRegex = /^[a-zA-Z0-9-]+$/;

  // Testing "10023456-AX @" will return FALSE because of the space and @ symbols
  return policyIdRegex.test(cellValue);
}

function getFormattedValue(jsonKey, value) {
  const STRING_CONVERSION_KEYS = new Set([
    "chart_number",
    "policy_number",
    "tax_id",
    "npi",
    "pbm_phone_number",
    "auth_dept_phone_number",
    "tpa_number_id",
    "call_reference_number",
    "cpt_code",
    "icd_10_code",
    "copay",
    "coinsurance",
  ]);

  // Automatic date formatting to YYYY-MM-DD string for JSON compliance

  if (typeof value === "number" && STRING_CONVERSION_KEYS.has(jsonKey)) {
    value = String(value);
  }
  if (jsonKey === "phone_number") {
    return "+" + value;
  }

  return value;
}

function resolveRefs(sheet, obj) {
  if (typeof obj === "object" && obj !== null && !Array.isArray(obj)) {
    const newObj = {};
    let mandatory = false;
    for (let key in obj) {
      //to check if the fields required or not except cycle_limit that is optional
      if (mandatory && key !== "cycle_limit") {
        //requiredCells.push(obj[key].toUpperCase());
        obj[key][1] = true;
      }
      const keyValue = resolveRefs(sheet, obj[key]);
      if (keyValue !== "") {
        newObj[key] = getFormattedValue(key, keyValue);
      }

      //Logger.log(`the key and value is : ${key}, ${newObj[key]}`);
      // if (key === "covered" && newObj[key] === "Yes") {
      //   mandatory = true;
      // }
    }
    return newObj;
  } else if (Array.isArray(obj) && /^[A-Z]+\d+$/.test(obj[0])) {
    const value = sheet.getRange(obj[0]).getValue();
    //check missing fields and add to the list
    if (obj[1] === true && !value) {
      //Mark missing required cells
      missingCells.push(obj[0].toUpperCase());
    }
    return value;
  } else {
    return obj;
  }
}

// Name used for the hidden sheet where properties are temporarily stored.
const PROPERTIES_TRANSFER_SHEET_NAME = "___ScriptPropertiesTempStore";

// We use Script properties for configuration shared by all users of the script.
const PROPERTIES_SERVICE = PropertiesService.getScriptProperties();

/**
 * ===================================================================
 * 2. DUPLICATION FUNCTION (RUNS IN ORIGINAL TEMPLATE SHEET)
 * ===================================================================
 * This function is run via the menu. It reads properties, embeds them,
 * copies the file (including properties), and cleans up the original.
 */
function copyAndConfigureTemplate() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ui = SpreadsheetApp.getUi();
  const originalFileName = ss.getName();
  let tempSheet = null; // Declare outside of try to allow catch access

  try {
    // 1. READ all properties from the current script's store
    const allProperties = PROPERTIES_SERVICE.getProperties();

    if (Object.keys(allProperties).length === 0) {
      ui.alert(
        "Warning",
        "No properties found. Creating standard duplicate.",
        ui.ButtonSet.OK,
      );
    }

    // 2. EMBED properties into a HIDDEN sheet
    tempSheet = ss.insertSheet(PROPERTIES_TRANSFER_SHEET_NAME);
    tempSheet.hideSheet();
    // Write properties as a string to A1
    tempSheet.getRange("A1").setValue(JSON.stringify(allProperties));

    // --- OPTIMIZED RACE CONDITION DEFENSE ---
    // Forces all pending spreadsheet updates (writing to A1) to commit immediately.
    SpreadsheetApp.flush();
    // Pauses execution for 1 second to guarantee the file system update is complete before copying.
    Utilities.sleep(1000);
    // ------------------------------------------

    // 3. COPY the spreadsheet file (which now includes the committed hidden sheet data)
    const newFileName = originalFileName + " (Copy - Configured)";
    const copiedFile = DriveApp.getFileById(ss.getId()).makeCopy(newFileName);

    // 4. CLEAN UP the original sheet
    ss.deleteSheet(tempSheet);
    tempSheet = null; // Mark as cleaned up

    // 5. NOTIFY the user
    // const successMessage = `✅ Success! Your configured copy has been created:\n\n` +
    //                        `File: ${newFileName}\n` +
    //                        `URL: ${copiedFile.getUrl()}\n\n` +
    //                        `Please open the new sheet and click the menu item:\n` +
    //                        `'⚙️ Cop & Config' -> 'CLICK TO ACTIVATE SCRIPT PROPERTIES'`;

    // ui.alert("Template Copy Complete", successMessage, ui.ButtonSet.OK);

    const template = HtmlService.createTemplateFromFile("SheetOpener");
    template.fullUrl = copiedFile.getUrl(); // Pass the URL to the HTML template

    const htmlOutput = template
      .evaluate()
      .setWidth(300) // Make the dialog minimal/invisible
      .setHeight(150);

    // htmlOutput.setSandboxMode(HtmlService.SandboxMode.IFRAME);

    // Execute the client-side function with the URL
    //ss.toast('Sheet duplicated! Attempting to open new tab...', 'Success', 3);
    SpreadsheetApp.getUi().showModelessDialog(htmlOutput, "Open New Sheet");

    // Optionally, set the new sheet as the active one *after* the dialog is launched
    // ss.setActiveSheet(copiedFile);
  } catch (e) {
    // Robust Error Handling: Ensure the temp sheet is deleted if the copy fails
    if (tempSheet !== null) {
      ss.deleteSheet(tempSheet);
    }
    Logger.log("Error during sheet duplication: " + e.toString());
    ui.alert(
      "Error During Copy",
      `Failed to create copy: ${e.toString()}`,
      ui.ButtonSet.OK,
    );
  }
}

/**
 * ===================================================================
 * 3. INITIALIZATION FUNCTION (RUNS IN NEW DUPLICATE SHEET - MANUALLY)
 * ===================================================================
 * This function must be called manually by the user in the new sheet
 * to authorize the script and perform the property setup.
 */
function initializeProperties() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ui = SpreadsheetApp.getUi();

  Logger.log("--- STARTING INITIALIZE PROPERTIES ---");

  // 1. LOOK for the hidden sheet
  const tempSheet = ss.getSheetByName(PROPERTIES_TRANSFER_SHEET_NAME);

  if (tempSheet) {
    try {
      // Get the JSON string from A1
      const propertiesJson = tempSheet.getRange("A1").getValue();

      if (propertiesJson) {
        // Credential-safe: this JSON carries real secrets (EXTERNAL_*_API_KEY
        // bearer tokens) — never log its raw contents or the parsed object,
        // only that it was found and which property names it carries.
        Logger.log("STEP 2: Found properties JSON.");

        // 2. READ & Parse the properties
        const propertiesToSet = JSON.parse(propertiesJson);
        Logger.log(
          "STEP 3: Successfully parsed object with keys: " +
            Object.keys(propertiesToSet).join(", "),
        );

        // 3. SET the properties in the *new* script's Properties Service
        PROPERTIES_SERVICE.setProperties(propertiesToSet, true);
        Logger.log(
          "STEP 4: Properties set successfully in new script storage.",
        );

        // 4. CLEAN UP: Delete the temporary sheet for security
        ss.deleteSheet(tempSheet);
        Logger.log("STEP 5: Cleanup successful (temp sheet deleted).");

        // 5. NOTIFY the user of successful configuration
        //ui.alert('✅ Properties successfully transferred and script is now fully configured!', 'Setup Complete', ui.ButtonSet.OK);
      } else {
        Logger.log("ERROR: A1 in the temp sheet was empty.");
        ui.alert(
          "Configuration Error",
          "Temporary configuration data was empty. Properties not set.",
          ui.ButtonSet.OK,
        );
      }
    } catch (e) {
      Logger.log("FATAL ERROR DURING INITIALIZATION: " + e.toString());
      ui.alert(
        "Configuration Error",
        "Failed to process properties. Check the Apps Script Logs for details.",
        ui.ButtonSet.OK,
      );
    }
  } else {
    //ss.toast('Already activated script properties.', 'Status', 3);
    Logger.log(
      "STEP 1: Temporary configuration sheet was not found. Template likely already initialized.",
    );
  }
  Logger.log("--- ENDING INITIALIZE PROPERTIES ---");
}

/**
 * ===================================================================
 * 4. ON OPEN TRIGGER
 * ===================================================================
 * This is a simple trigger, only used to create the menu.
 */
function onOpen() {
  const ui = SpreadsheetApp.getUi();

  // Add custom menu items for both copying (in original) and initialization (in copy)
  ui.createMenu("⚙️ Copy This Sheet")
    .addItem(
      "Generate Configured Copy of this Sheet",
      "copyAndConfigureTemplate",
    )
    .addToUi();
}
